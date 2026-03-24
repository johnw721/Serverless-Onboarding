import datetime
from ldap3 import Server, Connection, ALL
import urllib
import boto3
import ldap3
from helpers import validate_employee_data, log_onboarding_request, ROLE_TO_GROUPS_MAP, ROLE_TO_DEPARTMENT_MAP, ALL_EMPLOYEES_GROUP
from botocore.exceptions import ClientError
import os
import json
import logger

boto3_config = boto3.session.Config(connect_timeout=5, read_timeout=5)

### Use Claude via AWS Bedrock for natural language processing tasks, such as extracting user information from the event body
#  or generating notifications based on user roles and departments.
bedrock_client = boto3.client('bedrock', config=boto3_config)

### Initialize AWS Secrets Manager client
secretsmanager = boto3.client("secretsmanager", config=boto3_config)

notify_sns_lambda=boto3.client("lambda", config=boto3_config)

domain = os.getenv("DOMAIN")  # Assuming the domain is set as an environment variable
base_dn = os.getenv("BASE_DN")  # Assuming the base DN is set as an environment variable


### Initialize AWS Active Directory client
# ad_client = boto3.client('ds-data', config=boto3.session.Config(connect_timeout=5, read_timeout=5))
### Explanation: The connect_timeout and read_timeout parameters in the boto3 session configuration are set to
#  5 seconds each to ensure that the Lambda function does not wait indefinitely for a response from the Active Directory service. 
# This is important in a serverless environment like AWS Lambda, where functions are designed to be short-lived and responsive. 
# Setting these timeouts helps to prevent the function from hanging and allows it to fail gracefully
#  if the Active Directory service is unresponsive or slow, ensuring better performance and reliability.



### Verify response from Slack and extract necessary information for user creation
def lambda_handler(event, _):


### Define LDAP server and connection parameters
    ldap_server = secretsmanager.get_secret_value("ldap_server_address").get("SecretString")
    ldap_user = secretsmanager.get_secret_value("ldap_username").get("SecretString")
    ldap_password = secretsmanager.get_secret_value("ldap_password").get("SecretString")

# Parse the incoming event body to extract user information, handling both JSON and URL-encoded formats
    raw_body = event["body"]
    decoded_body = urllib.parse.unquote_plus(raw_body)
    # outputs something like this: username=jdoe&name=John+Doe&Role=Software+Engineer&Department=Engineering
    try:
        user_info = json.loads(decoded_body)
    # outputs something like this: {'username': 'jdoe', 'name': 'John Doe', 'Role': 'Software Engineer', 'Department': 'Engineering'}
    except json.JSONDecodeError:
        parsed = urllib.parse.parse_qs(decoded_body)
    # outputs something like this: {'username': ['jdoe'], 'name': ['John Doe'], 'Role': ['Software Engineer'], 'Department': ['Engineering']}
        user_info = {k: v[0] for k, v in parsed.items()}
    # outputs this final dictionary: {'username': 'jdoe', 'name': 'John Doe', 'Role': 'Software Engineer', 'Department': 'Engineering'}

# Now safe to validate
    # Create a new user in Active Directory using the LDAP connection
    try:
        ### Establish connection to the LDAP server
        server = Server(ldap_server, get_info=ALL)
        conn = Connection(server, user=ldap_user, password=ldap_password, client_strategy='SYNC', use_ssl=True)
        # Connect to the LDAP server and validate the user information before creating the user
        conn.bind()
        validate_employee_data(user_info)
        # Example of creating a new user (this is a simplified example, actual implementation may vary)
        conn.add(f"CN={user_info['username']},{base_dn}", ['top', 'person', 'organizationalPerson', 'user'], {
            'sAMAccountName': user_info['username'],
            'userPrincipalName': f"{user_info['username']}@{domain}",
            'displayName': user_info['name'],
            'department': ROLE_TO_DEPARTMENT_MAP.get(user_info["Role"], "General"),
            'title': user_info["Role"]
        })
        # Add user to appropriate groups based on their role
        groups_to_add = ROLE_TO_GROUPS_MAP.get(user_info["Role"], [])
        for _ in groups_to_add:
            conn.modify(f"CN={ALL_EMPLOYEES_GROUP},{base_dn}", {'member': [(ldap3.MODIFY_ADD, [f"CN={user_info['username']},{base_dn}"])]})
        # Return a success response if the user was created successfully

        conn.unbind()
        # Log the onboarding request as successful using dynamodb directly from the lambda function to ensure that the log is recorded even if there are issues with the SNS notification
        log_onboarding_request(user_info, status="Success")
        # Send a notification to SNS about the successful onboarding (inside of another lambda function to decouple the logic and improve maintainability)
        try:
            notify_sns_lambda.invoke(
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
        return {
            "statusCode": 200,
            "body": json.dumps({"message": f"User {user_info['username']} onboarded successfully."})
        }
    except Exception as e:
        log_onboarding_request(user_info, status="Failed")
        try:
            notify_sns_lambda.invoke(
                FunctionName= f"{os.getenv('NOTIFY_SNS_LAMBDA_NAME')}",  # Assuming the name of the SNS notification lambda function is set as an environment variable
                InvocationType='Event',  # Asynchronous invocation
                Payload=json.dumps({
                    "topic_arn": f"{os.getenv('SNS_TOPIC_ARN')}",  # Assuming the SNS topic ARN is set as an environment variable
                    "message": f"Failed to onboard user {user_info['username']} with role {user_info['Role']} in department {user_info['Department']}.",
                    "subject": "New Employee Onboarding Failure"
                })
            )
        except ClientError as notify_error:
            print(f"Failed to invoke notification lambda: {notify_error}")
        return {
            "statusCode": 500,
            "body": json.dumps({"message": f"Failed to onboard user. Error: {str(e)}"})
        }
    