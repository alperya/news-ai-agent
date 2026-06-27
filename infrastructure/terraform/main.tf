terraform {
  required_version = ">= 1.0"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 5.0" }
  }
  backend "s3" {}
}

provider "aws" {
  region = var.aws_region
}

variable "aws_region" {
  description = "AWS region"
  type        = string
  default     = "eu-central-1" # Frankfurt - closest to Amsterdam
}

variable "project_name" {
  description = "Project name"
  type        = string
  default     = "news-ai-agent"
}

variable "lambda_timeout" {
  description = "Lambda timeout in seconds"
  type        = number
  default     = 900 # 15 minutes - video rendering on Lambda needs generous timeout
}

variable "lambda_memory" {
  description = "Lambda memory in MB"
  type        = number
  default     = 3008 # ~3 GB (Lambda max) - video rendering needs RAM + more memory = more Lambda CPU
}

variable "alert_email" {
  description = "Email address for error alerts (leave empty to disable)"
  type        = string
  default     = ""
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

resource "aws_s3_bucket_lifecycle_configuration" "results_lifecycle" {
  bucket = aws_s3_bucket.results.id

  rule {
    id     = "delete-errors-after-30-days"
    status = "Enabled"

    filter {
      prefix = "errors/"
    }

    expiration {
      days = 30
    }

    noncurrent_version_expiration {
      noncurrent_days = 30
    }
  }

  rule {
    id     = "archive-analytics-after-90-days"
    status = "Enabled"

    filter {
      prefix = "analytics/"
    }

    transition {
      days          = 90
      storage_class = "GLACIER"
    }
  }

  rule {
    id     = "archive-account-metrics-after-365-days"
    status = "Enabled"

    filter {
      prefix = "metrics/account/"
    }

    transition {
      days          = 365
      storage_class = "GLACIER"
    }
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

# ===== SNS Topic for Error Alerts =====
resource "aws_sns_topic" "alerts" {
  count = var.alert_email != "" ? 1 : 0
  name  = "${var.project_name}-alerts"

  tags = {
    Name        = "${var.project_name}-alerts"
    Environment = "production"
    ManagedBy   = "terraform"
  }
}

resource "aws_sns_topic_subscription" "alert_email" {
  count     = var.alert_email != "" ? 1 : 0
  topic_arn = aws_sns_topic.alerts[0].arn
  protocol  = "email"
  endpoint  = var.alert_email
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
          "s3:GetObject",
          "s3:DeleteObject"
        ]
        Resource = "${aws_s3_bucket.results.arn}/*"
      },
      {
        Effect = "Allow"
        Action = [
          "s3:ListBucket"
        ]
        Resource = aws_s3_bucket.results.arn
      },
      {
        Effect = "Allow"
        Action = [
          "secretsmanager:GetSecretValue",
          "secretsmanager:PutSecretValue"
        ]
        Resource = aws_secretsmanager_secret.credentials.arn
      },
      {
        Effect = "Allow"
        Action = [
          "sns:Publish"
        ]
        Resource = "arn:aws:sns:${var.aws_region}:${data.aws_caller_identity.current.account_id}:${var.project_name}-*"
      },
      {
        Effect = "Allow"
        Action = [
          "lambda:InvokeFunction"
        ]
        Resource = [
          "arn:aws:lambda:${var.aws_region}:${data.aws_caller_identity.current.account_id}:function:${var.project_name}-reels-publish",
          "arn:aws:lambda:${var.aws_region}:${data.aws_caller_identity.current.account_id}:function:${var.project_name}-youtube-publish",
        ]
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

# ===== CloudWatch Alarm for Lambda Errors (OOM, Timeout, Crashes) =====
resource "aws_cloudwatch_metric_alarm" "lambda_errors" {
  count               = var.alert_email != "" ? 1 : 0
  alarm_name          = "${var.project_name}-lambda-errors"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "Errors"
  namespace           = "AWS/Lambda"
  period              = 300
  statistic           = "Sum"
  threshold           = 0
  alarm_description   = "Alert on Lambda errors — OOM, timeout, unhandled crashes"
  treat_missing_data  = "notBreaching"

  dimensions = {
    FunctionName = var.project_name
  }

  alarm_actions = [aws_sns_topic.alerts[0].arn]

  tags = {
    Name        = "${var.project_name}-error-alarm"
    Environment = "production"
    ManagedBy   = "terraform"
  }
}

# ===== CloudWatch Alarm — Lambda approaching 15-min timeout =====
# Duration metric is in milliseconds; 840 000 ms = 14 minutes.
# Fires if any single invocation runs longer than 14 min (1 min before the hard kill).
resource "aws_cloudwatch_metric_alarm" "lambda_duration" {
  count               = var.alert_email != "" ? 1 : 0
  alarm_name          = "${var.project_name}-lambda-slow"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "Duration"
  namespace           = "AWS/Lambda"
  period              = 300
  statistic           = "Maximum"
  threshold           = 840000
  alarm_description   = "Lambda runtime exceeded 14 min — approaching 15-min hard timeout"
  treat_missing_data  = "notBreaching"

  dimensions = {
    FunctionName = var.project_name
  }

  alarm_actions = [aws_sns_topic.alerts[0].arn]

  tags = {
    Name        = "${var.project_name}-slow-alarm"
    Environment = "production"
    ManagedBy   = "terraform"
  }
}

# ===== Guaranteed failure alerting — async on-failure destinations =====
# CloudWatch metric alarms (above) proved unreliable for these functions: the
# main Lambda can run up to 15 min, so its Errors/Duration datapoints arrive
# long after the period starts, and with a sparse metric + notBreaching the
# alarm can miss the breach entirely (a real 15-min timeout went un-alerted).
# Every function here is invoked ASYNCHRONOUSLY (EventBridge → Lambda, or
# main → worker with InvocationType=Event), so an on-failure *destination* is
# the timing-independent guarantee: Lambda delivers the failed invocation
# (including timeouts/OOM) to the SNS alerts topic → email. The shared
# execution role already holds sns:Publish to news-ai-agent-* topics.
#
# maximum_retry_attempts: the heavy main render function retries are set to 0 —
# a timeout simply re-times-out (we observed two wasted 15-min runs) and the
# next scheduled slot recovers, so we alert immediately instead of after 3×.
locals {
  failure_alert_lambdas = var.alert_email != "" ? {
    main              = { name = aws_lambda_function.news_agent.function_name, retries = 0 }
    reels_publish     = { name = aws_lambda_function.reels_publish.function_name, retries = 2 }
    youtube_publish   = { name = aws_lambda_function.youtube_publish.function_name, retries = 2 }
    token_refresh     = { name = aws_lambda_function.token_refresh.function_name, retries = 2 }
    metrics_collector = { name = aws_lambda_function.metrics_collector.function_name, retries = 2 }
    analytics_engine  = { name = aws_lambda_function.analytics_engine.function_name, retries = 2 }
  } : {}
}

resource "aws_lambda_function_event_invoke_config" "failure_alert" {
  for_each               = local.failure_alert_lambdas
  function_name          = each.value.name
  maximum_retry_attempts = each.value.retries

  destination_config {
    on_failure {
      destination = aws_sns_topic.alerts[0].arn
    }
  }
}

# ===== Lambda Deployment Package =====
# Uploads the locally-built ZIP to S3 so all Lambda functions can reference it.
# Terraform detects changes via etag (MD5) and re-uploads only when the ZIP changes.
resource "aws_s3_object" "lambda_zip" {
  bucket = aws_s3_bucket.results.id
  key    = "deployments/lambda_deployment.zip"
  source = "../../lambda_deployment.zip"
  etag   = filemd5("../../lambda_deployment.zip")
}

# ===== Lambda Function =====
resource "aws_lambda_function" "news_agent" {
  s3_bucket     = aws_s3_bucket.results.id
  s3_key        = aws_s3_object.lambda_zip.key
  function_name = var.project_name
  role          = aws_iam_role.lambda_role.arn
  handler       = "lambda_handler.lambda_handler"
  runtime       = "python3.12"
  timeout       = var.lambda_timeout
  memory_size   = var.lambda_memory

  source_code_hash = filebase64sha256("../../lambda_deployment.zip")

  environment {
    variables = {
      RESULTS_BUCKET                = aws_s3_bucket.results.id
      SECRET_NAME                   = aws_secretsmanager_secret.credentials.name
      SNS_ALERT_TOPIC_ARN           = var.alert_email != "" ? aws_sns_topic.alerts[0].arn : ""
      REELS_PUBLISH_FUNCTION_NAME   = "${var.project_name}-reels-publish"
      YOUTUBE_PUBLISH_FUNCTION_NAME = "${var.project_name}-youtube-publish"
    }
  }

  tags = {
    Name        = var.project_name
    Environment = "production"
    ManagedBy   = "terraform"
  }

  depends_on = [
    aws_iam_role_policy.lambda_policy,
    aws_cloudwatch_log_group.lambda_logs,
    aws_s3_object.lambda_zip,
  ]
}

# ===== Lambda Function — Reels Publisher (async, invoked by news-agent) =====
resource "aws_lambda_function" "reels_publish" {
  s3_bucket     = aws_s3_bucket.results.id
  s3_key        = aws_s3_object.lambda_zip.key
  function_name = "${var.project_name}-reels-publish"
  role          = aws_iam_role.lambda_role.arn
  handler       = "reels_worker.lambda_handler"
  runtime       = "python3.12"
  timeout       = 600 # 10 minutes: Meta polling up to 80×8s=640s, capped here
  memory_size   = 256 # no video processing — minimal RAM needed

  source_code_hash = filebase64sha256("../../lambda_deployment.zip")

  environment {
    variables = {
      RESULTS_BUCKET      = aws_s3_bucket.results.id
      SECRET_NAME         = aws_secretsmanager_secret.credentials.name
      SNS_ALERT_TOPIC_ARN = var.alert_email != "" ? aws_sns_topic.alerts[0].arn : ""
    }
  }

  tags = {
    Name        = "${var.project_name}-reels-publish"
    Environment = "production"
    ManagedBy   = "terraform"
  }

  depends_on = [
    aws_iam_role_policy.lambda_policy,
    aws_s3_object.lambda_zip,
  ]
}

resource "aws_cloudwatch_log_group" "reels_publish_logs" {
  name              = "/aws/lambda/${var.project_name}-reels-publish"
  retention_in_days = 7

  tags = {
    Name        = "${var.project_name}-reels-publish-logs"
    Environment = "production"
    ManagedBy   = "terraform"
  }
}

# ===== Lambda Function — YouTube Shorts Publisher (async, invoked by news-agent) =====
resource "aws_lambda_function" "youtube_publish" {
  s3_bucket     = aws_s3_bucket.results.id
  s3_key        = aws_s3_object.lambda_zip.key
  function_name = "${var.project_name}-youtube-publish"
  role          = aws_iam_role.lambda_role.arn
  handler       = "youtube_worker.lambda_handler"
  runtime       = "python3.12"
  timeout       = 300 # 5 minutes: video download (~10s) + YouTube upload (~2 min)
  memory_size   = 256 # no video rendering — minimal RAM needed

  source_code_hash = filebase64sha256("../../lambda_deployment.zip")

  environment {
    variables = {
      RESULTS_BUCKET      = aws_s3_bucket.results.id
      SECRET_NAME         = aws_secretsmanager_secret.credentials.name
      SNS_ALERT_TOPIC_ARN = var.alert_email != "" ? aws_sns_topic.alerts[0].arn : ""
    }
  }

  tags = {
    Name        = "${var.project_name}-youtube-publish"
    Environment = "production"
    ManagedBy   = "terraform"
  }

  depends_on = [
    aws_iam_role_policy.lambda_policy,
    aws_s3_object.lambda_zip,
  ]
}

resource "aws_cloudwatch_log_group" "youtube_publish_logs" {
  name              = "/aws/lambda/${var.project_name}-youtube-publish"
  retention_in_days = 7

  tags = {
    Name        = "${var.project_name}-youtube-publish-logs"
    Environment = "production"
    ManagedBy   = "terraform"
  }
}

# ===== EventBridge Rules (Amsterdam Time CEST = UTC+2, CET = UTC+1) =====
# Note: cron expressions are UTC. In winter (CET) these fire 1 hour later AMS time.
#
# Reel 1: 11:00 Amsterdam CEST = 09:00 UTC  (data-backed: highest reach amplification)
resource "aws_cloudwatch_event_rule" "morning_schedule" {
  name                = "${var.project_name}-morning"
  description         = "Reel 1 — 11:00 Amsterdam (09:00 UTC / CEST)"
  schedule_expression = "cron(0 9 * * ? *)"

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
    time     = "11:00"
    format   = "reels"
  })
}

# Reel 2: 19:00 Amsterdam CEST = 17:00 UTC  (after-work prime browsing window)
resource "aws_cloudwatch_event_rule" "afternoon_schedule" {
  name                = "${var.project_name}-afternoon"
  description         = "Reel 2 — 19:00 Amsterdam (17:00 UTC / CEST)"
  schedule_expression = "cron(0 17 * * ? *)"

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
    schedule = "evening"
    time     = "19:00"
    format   = "reels"
  })
}

# Evening photo slot removed — replaced by 2-Reels strategy above
resource "aws_cloudwatch_event_rule" "evening_schedule" {
  name                = "${var.project_name}-evening"
  description         = "Disabled — previously 17:30 photo post, replaced by 2-Reels schedule"
  schedule_expression = "cron(30 16 * * ? *)"
  state               = "DISABLED"

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

# Weekly events post: Wednesday 18:00 Amsterdam (17:00 UTC / CET)
resource "aws_cloudwatch_event_rule" "events_thursday_schedule" {
  name                = "${var.project_name}-events-thursday"
  description         = "NL events post — Thursday 18:00 Amsterdam (16:00 UTC / CEST). Thursday captures weekend + week-ahead planning intent with ticket lead time (utility content)."
  schedule_expression = "cron(0 16 ? * THU *)"
  state               = "ENABLED"

  tags = {
    Name        = "${var.project_name}-events-thursday"
    Environment = "production"
    ManagedBy   = "terraform"
  }
}

resource "aws_cloudwatch_event_target" "events_thursday_target" {
  rule      = aws_cloudwatch_event_rule.events_thursday_schedule.name
  target_id = "events-thursday-lambda"
  arn       = aws_lambda_function.news_agent.arn

  input = jsonencode({
    format = "event_post"
  })
}

resource "aws_cloudwatch_event_rule" "daily_fact_schedule" {
  name                = "${var.project_name}-daily-fact"
  description         = "Daily Dutch-fact Story — 08:00 Amsterdam (06:00 UTC / CEST). Fills the empty morning slot before the 11:00 Reel; Stories don't cannibalize Reel reach."
  schedule_expression = "cron(0 6 * * ? *)"
  state               = "ENABLED"

  tags = {
    Name        = "${var.project_name}-daily-fact"
    Environment = "production"
    ManagedBy   = "terraform"
  }
}

resource "aws_cloudwatch_event_target" "daily_fact_target" {
  rule      = aws_cloudwatch_event_rule.daily_fact_schedule.name
  target_id = "daily-fact-lambda"
  arn       = aws_lambda_function.news_agent.arn

  input = jsonencode({
    format = "daily_fact"
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

resource "aws_lambda_permission" "allow_events_thursday_eventbridge" {
  statement_id  = "AllowExecutionFromEventBridgeEventsThursday"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.news_agent.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.events_thursday_schedule.arn
}

resource "aws_lambda_permission" "allow_daily_fact_eventbridge" {
  statement_id  = "AllowExecutionFromEventBridgeDailyFact"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.news_agent.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.daily_fact_schedule.arn
}

# ===== Token Refresh Lambda =====
# Runs every 30 days to exchange the current Instagram long-lived token
# (60-day TTL) for a fresh one and write it back to Secrets Manager.

resource "aws_lambda_function" "token_refresh" {
  s3_bucket     = aws_s3_bucket.results.id
  s3_key        = aws_s3_object.lambda_zip.key
  function_name = "${var.project_name}-token-refresh"
  role          = aws_iam_role.lambda_role.arn
  handler       = "token_refresher.lambda_handler"
  runtime       = "python3.12"
  timeout       = 60
  memory_size   = 256

  source_code_hash = filebase64sha256("../../lambda_deployment.zip")

  environment {
    variables = {
      SECRET_NAME         = aws_secretsmanager_secret.credentials.name
      SNS_ALERT_TOPIC_ARN = var.alert_email != "" ? aws_sns_topic.alerts[0].arn : ""
    }
  }

  tags = {
    Name        = "${var.project_name}-token-refresh"
    Environment = "production"
    ManagedBy   = "terraform"
  }

  depends_on = [
    aws_iam_role_policy.lambda_policy,
    aws_s3_object.lambda_zip,
  ]
}

resource "aws_cloudwatch_log_group" "token_refresh_logs" {
  name              = "/aws/lambda/${var.project_name}-token-refresh"
  retention_in_days = 7

  tags = {
    Name        = "${var.project_name}-token-refresh-logs"
    Environment = "production"
    ManagedBy   = "terraform"
  }
}

resource "aws_cloudwatch_event_rule" "token_refresh_schedule" {
  name                = "${var.project_name}-token-refresh"
  description         = "Refresh Instagram access token every 30 days (token TTL is 60 days)"
  schedule_expression = "rate(30 days)"

  tags = {
    Name        = "${var.project_name}-token-refresh-schedule"
    Environment = "production"
    ManagedBy   = "terraform"
  }
}

resource "aws_cloudwatch_event_target" "token_refresh_target" {
  rule      = aws_cloudwatch_event_rule.token_refresh_schedule.name
  target_id = "token-refresh-lambda"
  arn       = aws_lambda_function.token_refresh.arn
}

resource "aws_lambda_permission" "allow_token_refresh_eventbridge" {
  statement_id  = "AllowExecutionFromEventBridgeTokenRefresh"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.token_refresh.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.token_refresh_schedule.arn
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

output "sns_alert_topic_arn" {
  description = "SNS topic ARN for error alerts"
  value       = var.alert_email != "" ? aws_sns_topic.alerts[0].arn : "disabled"
}
