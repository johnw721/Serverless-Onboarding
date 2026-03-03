resource "aws_directory_service_directory" "bar" {
  name     = "business.abc.com"
  password = aws_secretsmanager_secret_version.directory_password_version.secret_string
  size     = "Small"
  type     = "SimpleAD"

### Basic Network Settings
  vpc_settings {
    vpc_id     = aws_vpc.main.id
    subnet_ids = [aws_subnet.foo.id]
  }

  tags = {
    Project = "AD_Lambda_Onboarding"
  }
}

resource "aws_vpc" "main" {
  cidr_block = "10.0.0.0/16"
}

resource "aws_subnet" "foo" {
  vpc_id            = aws_vpc.main.id
  availability_zone = "us-west-2a"
  cidr_block        = "10.0.1.0/24"
}

### Secrets Manager to store OpenAI API Key to make calls
resource "aws_secretsmanager_secret" "openapi_secret" {
  name = "api_secret"
}

### Secrets Manager to store Directory Service Admin Password
resource "aws_secretsmanager_secret" "directory_password" {
  name = "directory_password"
}

resource "aws_secretsmanager_secret_version" "directory_password_version" {
  secret_id     = aws_secretsmanager_secret.directory_password.id
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

output "dynamodb_table_name" {
  value = aws_dynamodb_table.onboarding_request_table.name
}

### API Gateway for Onboarding Requests
resource "aws_apigatewayv2_api" "onboarding_api" {
  name          = "onboarding_api"
  protocol_type = "HTTPS"
}
output "api_endpoint" {
  value = aws_apigatewayv2_api.onboarding_api.api_endpoint
}

### SNS Topic for Notifications
resource "aws_sns_topic" "notification_topic" {
  name = "notification_topic"
}

### Turn arn of sns topic into a variable for use in Lambda function
output "sns_topic_arn" {
  value = aws_sns_topic.notification_topic.arn
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
          "secretsmanager:GetSecretValue",
          "dynamodb:PutItem",
          "sns:Publish",
          "ds:CreateUser"
        ]
        Effect   = "Allow"
        Resource = "*"
      },
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

### Attach AWSLambdaBasicExecutionRole to the Lambda Role
resource "aws_iam_role_policy_attachment" "lambda_basic_execution" {
  role       = aws_iam_role.lambda_role.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

### Lambda Function for Onboarding
resource "aws_lambda_function" "onboarding_function" {
  filename         = "Lambda_func.py"
  function_name    = "onboarding_function"
  role             = aws_iam_role.lambda_role.arn
  handler          = "Lambda_func.lambda_handler"
  runtime          = "python3.9"
  source_code_hash = filebase64sha256("Lambda_func.py")
  environment {
    variables = {
      DIRECTORY_ID       = aws_directory_service_directory.bar.id
      DYNAMODB_TABLE     = aws_dynamodb.onboarding_request_table.name
      SNS_TOPIC_ARN      = aws_sns_topic.notification_topic.arn
      OPENAI_API_KEY     = aws_secretsmanager_secret.api_secret.arn
      DIRECTORY_PASSWORD = aws_secretsmanager_secret.directory_password.arn
    }
  }
  depends_on = [aws_iam_role.lambda_role]
}