resource "aws_vpc" "main" {
  cidr_block = "10.0.0.0/16"
}

resource "aws_subnet" "foo" {
  vpc_id            = aws_vpc.main.id
  availability_zone = "us-west-2a"
  cidr_block        = "10.0.1.0/24"
  ### make private
  map_public_ip_on_launch = false
}


### API Gateway for Onboarding Requests
resource "aws_apigatewayv2_api" "onboarding_api" {
  name          = "onboarding_api"
  protocol_type = "HTTPS"
}


### API Gateway Integration with Lambda
resource "aws_apigatewayv2_integration" "lambda_integration" {
  api_id           = aws_apigatewayv2_api.onboarding_api.id
  integration_type = "AWS_PROXY"
  integration_uri  = aws_lambda_function.onboarding_function.invoke_arn
  ## Add credentials for API Gateway to invoke Lambda function
  credentials_arn = aws_iam_role.lambda_role.arn
  depends_on = [aws_lambda_function.onboarding_function]

}

### API Gateway Route
resource "aws_apigatewayv2_route" "onboarding_route" {
  api_id    = aws_apigatewayv2_api.onboarding_api.id
  route_key = "POST /onboard"
  target    = "integrations/${aws_apigatewayv2_integration.lambda_integration.id}"
}

### Setting up Service Account for Lambda to interact with Directory Service
resource "aws_iam_user" "lambda_service_account" {
  name = "lambda_service_account"
}

### Attach the AWSLambdaBasicExecutionRole to the Lambda Service Account
resource "aws_iam_user_policy_attachment" "lambda_service_account_basic_execution" {
  user       = aws_iam_user.lambda_service_account.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

### Attach custom policy to allow Lambda to interact with Directory Service, Secrets Manager, DynamoDB, and SNS
resource "aws_iam_user_policy_attachment" "lambda_service_account_custom_policy" {
  user       = aws_iam_user.lambda_service_account.name
  policy_arn = aws_iam_policy.lambda_policy.arn
}

### IAM Role for Lambda Functions
resource "aws_iam_role" "lambda_role" {
  name = "lambda_role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
      Principal = {
          Service = "lambda.amazonaws.com"
      }
      },
    ]
  })
}

### IAM Policy for access to Secrets Manager, DynamoDB, SNS, and Directory Service
resource "aws_iam_policy" "lambda_policy" {
  name = "lambda_policy"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = [
          "secretsmanager:GetSecretValue"
        ]
        Effect   = "Allow"
        Resource = [
          aws_secretsmanager_secret.openapi_secret.arn,
          aws_secretsmanager_secret.directory_password.arn
        ]
      },
      {
        Action = [
          "dynamodb:PutItem"
        ]
        Effect   = "Allow"
        Resource = aws_dynamodb_table.onboarding_request_table.arn
      },
      {
        Action = [
          "sns:Publish"
        ]
        Effect   = "Allow"
        Resource = aws_sns_topic.notification_topic.arn
      },
      {
        Action = [
          "ds:CreateUser"
        ]
        Effect   = "Allow"
        Resource = "*"
      }
    ]
  })
}

### Add Data Source to zip the Lambda Function Code
data "archive_file" "lambda_zip" {
  type        = "zip"
  source_file = "Lambda_func.py"
  output_path = "Lambda_func.zip"
}

### Attach the IAM Policy to the Role
resource "aws_iam_role_policy_attachment" "lambda_role_attachment" {
  role       = aws_iam_role.lambda_role.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

### Lambda Function for Onboarding
resource "aws_lambda_function" "onboarding_function" {
  filename         = data.archive_file.lambda_zip.output_path
  function_name    = "onboarding_function"
  role             = aws_iam_role.lambda_role.arn
  handler          = "Lambda_func.lambda_handler"
  runtime          = "python3.9"
  source_code_hash = data.archive_file.lambda_zip.output_base64sha256
  environment {
    variables = {
      DIRECTORY_ID       = aws_directory_service_directory.bar.id
      DYNAMODB_TABLE     = aws_dynamodb.onboarding_request_table.name
      SNS_TOPIC_ARN      = aws_sns_topic.notification_topic.arn
      OPENAI_API_KEY     = aws_secretsmanager_secret.api_secret.arn
      DIRECTORY_PASSWORD = aws_secretsmanager_secret.directory_password.arn
      DOMAIN = aws_directory_service_directory.bar.name
      BASE_DN = "DC=business,DC=abc,DC=com"
    }
  }
  depends_on = [aws_iam_role.lambda_role]
}

### Attachment of VPC Endpoint to Lambda Security Group
resource "aws_security_group" "lambda_sg" {
  name        = "lambda_sg"
  description = "Security group for Lambda function"
  vpc_id      = aws_vpc.main.id
}

### Allow Lambda to access VPC Endpoints
resource "aws_security_group_rule" "lambda_sg_rule" {
  type              = "egress"
  from_port         = 0
  to_port           = 0
  protocol          = "-1"
  security_group_id = aws_security_group.lambda_sg.id
  cidr_blocks       = "10.0.0.0/16"
  description       = "Allow Lambda to access VPC Endpoints"
}

### Secrets Manager to store OpenAI API Key to make calls
resource "aws_secretsmanager_secret" "openapi_secret" {
  name = "api_secret"
}

### Secrets Manager to store Directory Service Admin Password
resource "aws_secretsmanager_secret" "directory_password" {
  name = "directory_password"
}

### Secrets Manager Secret Version for Directory Service Admin Password
resource "aws_secretsmanager_secret_version" "directory_password_version" {
  secret_id     = aws_secretsmanager_secret.directory_password.id
  secret_string = "YourSecurePassword123!"
}

### Secrets Manager Rotation Schedule
resource "aws_secretsmanager_secret_rotation" "directory_password_rotation" {
  secret_id = aws_secretsmanager_secret.directory_password.id
  rotation_rules {
    automatically_after_days = 30
  }
}

### DynamoDB Table for Onboarding Requests
resource "aws_dynamodb_table" "onboarding_request_table" {
  name         = "onboarding_request_table"
  billing_mode = "PAY_PER_REQUEST"
  hash_key = "request_id"


  attribute {
    name = "request_id"
    type = "S"
  }
}


### vpc Interface Endpoint for Secrets Manager
resource "aws_vpc_endpoint" "secretsmanager_endpoint" {
  vpc_id            = aws_vpc.main.id
  service_name      = "com.amazonaws.us-west-2.secretsmanager"
  vpc_endpoint_type = "Interface"
  subnet_ids        = [aws_subnet.foo.id]
}

### Directory Service doesn't support VPC endpoints, so we will allow access to it through the security group attached to the Lambda function

### VPC Interface Endpoint for DynamoDB
resource "aws_vpc_endpoint" "dynamodb_endpoint" {

  vpc_id            = aws_vpc.main.id
  service_name      = "com.amazonaws.us-west-2.dynamodb"
  vpc_endpoint_type = "Interface"
  subnet_ids        = [aws_subnet.foo.id]
}

### VPC Interface Endpoint for SNS
resource "aws_vpc_endpoint" "sns_endpoint" {
  vpc_id            = aws_vpc.main.id
  service_name      = "com.amazonaws.us-west-2.sns"
  vpc_endpoint_type = "Interface"
  subnet_ids        = [aws_subnet.foo.id]
}


### SNS Topic for Notifications
resource "aws_sns_topic" "notification_topic" {
  name = "notification_topic"
}


### Directory Service Setup
resource "aws_directory_service_directory" "bar" {
  name     = "business.abc.com"
  password = aws_secretsmanager_secret_version.directory_password_version.secret_string
  size     = "Small"
  type     = "MicrosoftAD"

### Basic Network Settings
  vpc_settings {
    vpc_id     = aws_vpc.main.id
    subnet_ids = [aws_subnet.foo.id]
  }

  tags = {
    Project = "AD_Lambda_Onboarding"
  }
}
output "api_endpoint" {
  value = aws_apigatewayv2_api.onboarding_api.api_endpoint
}

output "dynamodb_table_name" {
  value = aws_dynamodb_table.onboarding_request_table.name
}

### Turn arn of sns topic into a variable for use in Lambda function
output "sns_topic_arn" {
  value = aws_sns_topic.notification_topic.arn
}