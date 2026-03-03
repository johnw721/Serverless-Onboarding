import boto3
import os
import time
import logger


ALL_EMPLOYEES_GROUP = "All Employees"

ROLE_TO_DEPARTMENT_MAP = {
    "Software Engineer": "Engineering",
    "Data Scientist": "Data Science",
    "Product Manager": "Product",
    "HR Specialist": "Human Resources",
    "Sales Executive": "Sales",
    "Marketing Manager": "Marketing",
    "Customer Support": "Support",
    "Finance Analyst": "Finance",
    "IT Administrator": "IT",
    "Operations Manager": "Operations",
}

ROLE_TO_GROUPS_MAP = {
    "Software Engineer": ["Engineering", ALL_EMPLOYEES_GROUP],
    "Data Scientist": ["Data Science", ALL_EMPLOYEES_GROUP],
    "Product Manager": ["Product", ALL_EMPLOYEES_GROUP],
    "HR Specialist": ["Human Resources", ALL_EMPLOYEES_GROUP],
    "Sales Executive": ["Sales", ALL_EMPLOYEES_GROUP],
    "Marketing Manager": ["Marketing", ALL_EMPLOYEES_GROUP],
    "Customer Support": ["Support", ALL_EMPLOYEES_GROUP],
    "Finance Analyst": ["Finance", ALL_EMPLOYEES_GROUP],
    "IT Administrator": ["IT", ALL_EMPLOYEES_GROUP],
    "Operations Manager": ["Operations", ALL_EMPLOYEES_GROUP],
}

def validate_employee_data(employee_data):
    required_fields = ["id", "name", "role", "department"]
    missing_fields = [field for field in required_fields if field not in employee_data]
    if missing_fields:
        raise ValueError(f"Missing required employee data fields: {', '.join(missing_fields)}")
    return True



def log_onboarding_request(employee_data, status):
    dynamodb = boto3.resource("dynamodb")
    table_name = dynamodb.Table(os.environ.get("DYNAMODB_TABLE_NAME"))
    table = dynamodb.Table(table_name)
    try:
        table.put_item(
            Item={
                "request_id": employee_data["id"],
                "employee_name": employee_data["name"],
                "timestamp": int(time.time()),
                "status": status
            }
        )
    except Exception as e:
        raise logger.error(f"Error logging onboarding request: {str(e)}")