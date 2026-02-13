terraform {
  required_version = ">= 1.0"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 5.0" }
  }
}

provider "aws" {
  region = var.aws_region
}

variable "aws_region" {
  description = "AWS region"
  type        = string
  default     = "eu-central-1"  # Frankfurt - closest to Amsterdam
}

variable "project_name" {
  description = "Project name"
  type        = string
  default     = "news-ai-agent"
}

variable "lambda_timeout" {
  description = "Lambda timeout in seconds"
  type        = number
  default     = 180  # 3 minutes - reduced from 300 for cost optimization
}

variable "lambda_memory" {
  description = "Lambda memory in MB"
  type        = number
  default     = 256  # 256 MB - reduced from 512 for cost optimization
}

data "aws_caller_identity" "current" {}

# ===== S3 Bucket for Results =====
resource "aws_s3_bucket" "results" {
  bucket = "${var.project_name}-results-${data.aws_caller_identity.current.account_id}"
  
  tags = {
    Name        = "${var.project_name}-results"
    Environment = "production"
    ManagedBy   = "terraform"
  }
}

resource "aws_s3_bucket_versioning" "results" {
  bucket = aws_s3_bucket.results.id
  
  versioning_configuration {
    status = "Enabled"
  }
}

# ===== Secrets Manager for Credentials =====
resource "aws_secretsmanager_secret" "credentials" {
  name        = "${var.project_name}/credentials"
  description = "Instagram and AI API credentials"
  
  tags = {
    Name        = "${var.project_name}-credentials"
    Environment = "production"
    ManagedBy   = "terraform"
  }
}

# ===== IAM Role for Lambda =====
resource "aws_iam_role" "lambda_role" {
  name = "${var.project_name}-lambda-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action = "sts:AssumeRole"
      Effect = "Allow"
      Principal = {
        Service = "lambda.amazonaws.com"
      }
    }]
  })
  
  tags = {
    Name        = "${var.project_name}-lambda-role"
    Environment = "production"
    ManagedBy   = "terraform"
  }
}

# ===== IAM Policy for Lambda =====
resource "aws_iam_role_policy" "lambda_policy" {
  name = "${var.project_name}-lambda-policy"
  role = aws_iam_role.lambda_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents"
        ]
        Resource = "arn:aws:logs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:*"
      },
      {
        Effect = "Allow"
        Action = [
          "s3:PutObject",
          "s3:GetObject"
        ]
        Resource = "${aws_s3_bucket.results.arn}/*"
      },
      {
        Effect = "Allow"
        Action = [
          "secretsmanager:GetSecretValue"
        ]
        Resource = aws_secretsmanager_secret.credentials.arn
      }
    ]
  })
}

# ===== CloudWatch Log Group =====
resource "aws_cloudwatch_log_group" "lambda_logs" {
  name              = "/aws/lambda/${var.project_name}"
  retention_in_days = 7
  
  tags = {
    Name        = "${var.project_name}-logs"
    Environment = "production"
    ManagedBy   = "terraform"
  }
}

# ===== Lambda Function =====
resource "aws_lambda_function" "news_agent" {
  filename         = "../../lambda_deployment.zip"
  function_name    = var.project_name
  role            = aws_iam_role.lambda_role.arn
  handler         = "lambda_handler.lambda_handler"
  runtime         = "python3.12"
  timeout         = var.lambda_timeout
  memory_size     = var.lambda_memory
  
  source_code_hash = filebase64sha256("../../lambda_deployment.zip")

  environment {
    variables = {
      RESULTS_BUCKET = aws_s3_bucket.results.id
      SECRET_NAME    = aws_secretsmanager_secret.credentials.name
    }
  }
  
  tags = {
    Name        = var.project_name
    Environment = "production"
    ManagedBy   = "terraform"
  }
  
  depends_on = [
    aws_iam_role_policy.lambda_policy,
    aws_cloudwatch_log_group.lambda_logs
  ]
}

# ===== EventBridge Rules (Amsterdam Time: UTC+1) =====
# Morning post: 08:00 Amsterdam = 07:00 UTC
resource "aws_cloudwatch_event_rule" "morning_schedule" {
  name                = "${var.project_name}-morning"
  description         = "Trigger Lambda at 08:00 Amsterdam time (07:00 UTC)"
  schedule_expression = "cron(0 7 * * ? *)"
  
  tags = {
    Name        = "${var.project_name}-morning-schedule"
    Environment = "production"
    ManagedBy   = "terraform"
  }
}

resource "aws_cloudwatch_event_target" "morning_target" {
  rule      = aws_cloudwatch_event_rule.morning_schedule.name
  target_id = "morning-lambda"
  arn       = aws_lambda_function.news_agent.arn
  
  input = jsonencode({
    schedule = "morning"
    time     = "08:00"
  })
}

# Afternoon post: 12:30 Amsterdam = 11:30 UTC
resource "aws_cloudwatch_event_rule" "afternoon_schedule" {
  name                = "${var.project_name}-afternoon"
  description         = "Trigger Lambda at 12:30 Amsterdam time (11:30 UTC)"
  schedule_expression = "cron(30 11 * * ? *)"
  
  tags = {
    Name        = "${var.project_name}-afternoon-schedule"
    Environment = "production"
    ManagedBy   = "terraform"
  }
}

resource "aws_cloudwatch_event_target" "afternoon_target" {
  rule      = aws_cloudwatch_event_rule.afternoon_schedule.name
  target_id = "afternoon-lambda"
  arn       = aws_lambda_function.news_agent.arn
  
  input = jsonencode({
    schedule = "afternoon"
    time     = "12:30"
  })
}

# Evening post: 17:30 Amsterdam = 16:30 UTC
resource "aws_cloudwatch_event_rule" "evening_schedule" {
  name                = "${var.project_name}-evening"
  description         = "Trigger Lambda at 17:30 Amsterdam time (16:30 UTC)"
  schedule_expression = "cron(30 16 * * ? *)"
  
  tags = {
    Name        = "${var.project_name}-evening-schedule"
    Environment = "production"
    ManagedBy   = "terraform"
  }
}

resource "aws_cloudwatch_event_target" "evening_target" {
  rule      = aws_cloudwatch_event_rule.evening_schedule.name
  target_id = "evening-lambda"
  arn       = aws_lambda_function.news_agent.arn
  
  input = jsonencode({
    schedule = "evening"
    time     = "17:30"
  })
}

# ===== Lambda Permissions for EventBridge =====
resource "aws_lambda_permission" "allow_morning_eventbridge" {
  statement_id  = "AllowExecutionFromEventBridgeMorning"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.news_agent.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.morning_schedule.arn
}

resource "aws_lambda_permission" "allow_afternoon_eventbridge" {
  statement_id  = "AllowExecutionFromEventBridgeAfternoon"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.news_agent.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.afternoon_schedule.arn
}

resource "aws_lambda_permission" "allow_evening_eventbridge" {
  statement_id  = "AllowExecutionFromEventBridgeEvening"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.news_agent.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.evening_schedule.arn
}

# ===== Outputs =====
output "lambda_function_name" {
  description = "Lambda function name"
  value       = aws_lambda_function.news_agent.function_name
}

output "lambda_function_arn" {
  description = "Lambda function ARN"
  value       = aws_lambda_function.news_agent.arn
}

output "s3_bucket_name" {
  description = "S3 bucket for results"
  value       = aws_s3_bucket.results.id
}

output "secrets_manager_secret_name" {
  description = "Secrets Manager secret name"
  value       = aws_secretsmanager_secret.credentials.name
}

output "cloudwatch_log_group" {
  description = "CloudWatch log group name"
  value       = aws_cloudwatch_log_group.lambda_logs.name
}
