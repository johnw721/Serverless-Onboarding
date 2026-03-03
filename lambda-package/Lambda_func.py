from ldap3 import Server, Connection, ALL
import urllib
import boto3
from helpers import validate_employee_data, log_onboarding_request, ROLE_TO_GROUPS_MAP, ROLE_TO_DEPARTMENT_MAP, ALL_EMPLOYEES_GROUP
from Nofity_SNS import notify_sns
from botocore.exceptions import ClientError


### Use Claude via AWS Bedrock for natural language processing tasks, such as extracting user information from the event body
#  or generating notifications based on user roles and departments.
bedrock_client = boto3.client('bedrock', config=boto3.session.Config(connect_timeout=5, read_timeout=5))

def ai_edge_case_handling(event_body):
    bedrock_client.invoke_model(
        ModelId='claude-2',
        Input={
            'text': event_body,
            'input_format': 'text'
        }    )


### Initialize AWS Secrets Manager client
secretsmanager = boto3.client("secretsmanager", config=boto3.session.Config(connect_timeout=5, read_timeout=5))

### Define LDAP server and connection parameters
ldap_server = secretsmanager.get_secret_value("ldap_server_address")
ldap_user = secretsmanager.get_secret_value("ldap_username")
ldap_password = secretsmanager.get_secret_value("ldap_password")

### Define SNS topic ARN for notifications
topic_arn = secretsmanager.get_secret_value("sns_topic_arn")

### Strategy Sync for a lambda function
### means the connection will be synchronous, suitable for short-lived operations like those in AWS Lambda functions.
### To Expand: Other strategies include ASYNC for asynchronous operations, RESTARTABLE for connections that can be paused and resumed, and LAZY for connections that delay operations until absolutely necessary.
### Out of these strategies, STRATEGY_SYNC is the most appropriate for AWS Lambda functions due to their short-lived nature and the need for immediate execution of LDAP operations.


### Establish connection to the LDAP server
server = Server(ldap_server, get_info=ALL)

### Initialize AWS Active Directory client
ad_client = boto3.client('activedirectory', config=boto3.session.Config(connect_timeout=5, read_timeout=5))
### Explanation: The connect_timeout and read_timeout parameters in the boto3 session configuration are set to
#  5 seconds each to ensure that the Lambda function does not wait indefinitely for a response from the Active Directory service. 
# This is important in a serverless environment like AWS Lambda, where functions are designed to be short-lived and responsive. 
# Setting these timeouts helps to prevent the function from hanging and allows it to fail gracefully
#  if the Active Directory service is unresponsive or slow, ensuring better performance and reliability.

conn = Connection(server, user=ldap_user, password=ldap_password, auto_bind=True, client_strategy='SYNC')

### Verify response from Slack and extract necessary information for user creation
def lambda_handler(event, context):
    event.body = urllib.parse.unquote_plus(event["body"])
    # Extract user information from the event body
    user_info = event["body"]
    # Create a new user in Active Directory using the LDAP connection
    try:
        # Connect to the LDAP server and validate the user information before creating the user
        conn.bind()
        validate_employee_data(user_info)
        # Example of creating a new user (this is a simplified example, actual implementation may vary)
        ad_client.create_user(
            UserName=user_info["username"],
            Role=user_info["Role"],
            Department=user_info["Department"]
            # Additional user attributes can be added here
        )
        # Add user to appropriate groups based on their role
        groups_to_add = ROLE_TO_GROUPS_MAP.get(user_info["Role"], [])
        for group in groups_to_add:
            ad_client.add_user_to_group(
                UserName=user_info["username"],
                GroupName=group
            )
        # Add user to the "All Employees" group
        ad_client.add_user_to_group(
            UserName=user_info["username"],
            GroupName=ALL_EMPLOYEES_GROUP
        )
        # Log the onboarding request as successful
        log_onboarding_request(user_info, status="Success")
        # Send a notification to SNS about the successful onboarding
        notify_sns(
            topic_arn=topic_arn,
            message=f"User {user_info['username']} has been successfully onboarded.",
            subject="New Employee Onboarding Success"
        )
    except Exception:
        print("There was an error")
        log_onboarding_request(user_info, status="Failed")
        notify_sns(
            topic_arn=topic_arn,
            message=f"Failed to onboard user {user_info['username']}.",
            subject="New Employee Onboarding Failure"
        )