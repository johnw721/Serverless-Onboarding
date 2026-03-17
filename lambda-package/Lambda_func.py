import datetime
from ldap3 import Server, Connection, ALL
import urllib
import boto3
from helpers import validate_employee_data, log_onboarding_request, ROLE_TO_GROUPS_MAP, ROLE_TO_DEPARTMENT_MAP, ALL_EMPLOYEES_GROUP
from botocore.exceptions import ClientError
import os


### Use Claude via AWS Bedrock for natural language processing tasks, such as extracting user information from the event body
#  or generating notifications based on user roles and departments.
bedrock_client = boto3.client('bedrock', config=boto3.session.Config(connect_timeout=5, read_timeout=5))

### Initialize AWS Secrets Manager client
secretsmanager = boto3.client("secretsmanager", config=boto3.session.Config(connect_timeout=5, read_timeout=5))

notify_sns=boto3.client("lambda")

domain = os.getenv("DOMAIN")  # Assuming the domain is set as an environment variable
base_dn = os.getenv("BASE_DN")  # Assuming the base DN is set as an environment variable


### Initialize AWS Active Directory client
ad_client = boto3.client('ds', config=boto3.session.Config(connect_timeout=5, read_timeout=5))
### Explanation: The connect_timeout and read_timeout parameters in the boto3 session configuration are set to
#  5 seconds each to ensure that the Lambda function does not wait indefinitely for a response from the Active Directory service. 
# This is important in a serverless environment like AWS Lambda, where functions are designed to be short-lived and responsive. 
# Setting these timeouts helps to prevent the function from hanging and allows it to fail gracefully
#  if the Active Directory service is unresponsive or slow, ensuring better performance and reliability.



### Verify response from Slack and extract necessary information for user creation
def lambda_handler(event, context):
    ### Define LDAP server and connection parameters
    ldap_server = secretsmanager.get_secret_value("ldap_server_address").get("SecretString")
    ldap_user = secretsmanager.get_secret_value("ldap_username").get("SecretString")
    ldap_password = secretsmanager.get_secret_value("ldap_password").get("SecretString")

    event.body = urllib.parse.unquote_plus(event["body"])
    # Extract user information from the event body
    user_info = event["body"]
    # Create a new user in Active Directory using the LDAP connection
    try:
        ### Establish connection to the LDAP server
        server = Server(ldap_server, get_info=ALL)
        conn = Connection(server, user=ldap_user, password=ldap_password, client_strategy='SYNC', use_ssl=True)
        # Connect to the LDAP server and validate the user information before creating the user
        conn.bind()
        validate_employee_data(user_info)
        # Example of creating a new user (this is a simplified example, actual implementation may vary)
        ad_client.create_user(
            userID = f"{user_info['username']}{datetime.datetime.now().strftime('%Y%m%d%H%M%S')}",  # Unique user ID based on username and timestamp
            userPrincipalName= f"{user_info['username']}+@{domain}.com",
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
        conn.unbind()
        # Log the onboarding request as successful using dynamodb directly from the lambda function to ensure that the log is recorded even if there are issues with the SNS notification
        log_onboarding_request(user_info, status="Success")
        # Send a notification to SNS about the successful onboarding (inside of another lambda function to decouple the logic and improve maintainability)
        try:
            notify_sns.invoke(
                FunctionName= f"{os.getenv('NOTIFY_SNS_LAMBDA_NAME')}",  # Assuming the name of the SNS notification lambda function is set as an environment variable
                InvocationType='Event',  # Asynchronous invocation
                Payload=json.dumps({
                    "topic_arn": f"{os.getenv('SNS_TOPIC_ARN')}",  # Assuming the SNS topic ARN is set as an environment variable
                    "message": f"Successfully onboarded user {user_info['username']} with role {user_info['Role']} in department {user_info['Department']}.",
                    "subject": "New Employee Onboarding Success"
                })
            )
        except ClientError as e:
            print(f"Failed to invoke notification lambda: {e}")
    except Exception:
        raise ClientError(
            {"Error": {"Code": "UserOnboardingError", "Message": "Failed to onboard user due to an error in the process."}},
            "CreateUser"
        )
    log_onboarding_request(user_info, status="Failed")
    try:
        notify_sns.invoke(
                FunctionName= f"{os.getenv('NOTIFY_SNS_LAMBDA_NAME')}",  # Assuming the name of the SNS notification lambda function is set as an environment variable
                InvocationType='Event',  # Asynchronous invocation
                Payload=json.dumps({
                    "topic_arn": f"{os.getenv('SNS_TOPIC_ARN')}",  # Assuming the SNS topic ARN is set as an environment variable
                    "message": f"Failed to onboard user {user_info['username']} with role {user_info['Role']} in department {user_info['Department']}.",
                    "subject": "New Employee Onboarding Failure"
                })
            )
    except ClientError as e:
        print(f"Failed to invoke notification lambda: {e}")
    