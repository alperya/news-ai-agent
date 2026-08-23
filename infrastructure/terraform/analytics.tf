# ===== Analytics Infrastructure =====
# DynamoDB tables, Glue/Athena for ad-hoc queries,
# CloudWatch Dashboard, and two new Lambda functions
# (metrics_collector + analytics_engine).

# ──────────────────────────────────────────────────────────
# DynamoDB — Per-post engagement metrics
# ──────────────────────────────────────────────────────────
resource "aws_dynamodb_table" "post_metrics" {
  name         = "${var.project_name}-post-metrics"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "post_id"

  attribute {
    name = "post_id"
    type = "S"
  }

  attribute {
    name = "post_type"
    type = "S"
  }

  attribute {
    name = "published_at"
    type = "S"
  }

  global_secondary_index {
    name            = "by_published_at"
    hash_key        = "post_type"
    range_key       = "published_at"
    projection_type = "ALL"
  }

  # 2-year retention. Before this the table had no TTL at all, so per-post
  # engagement accumulated forever while every reader only ever asks for the
  # last 7-30 days — and both readers (analytics_engine._load_posts,
  # selection_reviewer._load_metrics) use scan + FilterExpression, whose cost
  # tracks TOTAL table size rather than the window. Unbounded growth was
  # therefore a slowly rising bill on data nobody reads.
  #
  # DynamoDB only expires items that CARRY the attribute, so this is inert for
  # rows written before metrics_collector started stamping `expires_at`.
  # local_only/backfill_metrics_ttl.py stamps those once.
  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }

  tags = {
    Name        = "${var.project_name}-post-metrics"
    Environment = "production"
    ManagedBy   = "terraform"
  }
}

# ──────────────────────────────────────────────────────────
# DynamoDB — Prompt version history
# Enables rollback: copy old content back to Secrets Manager
# ──────────────────────────────────────────────────────────
resource "aws_dynamodb_table" "prompt_versions" {
  name         = "${var.project_name}-prompt-versions"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "prompt_name"
  range_key    = "version"

  attribute {
    name = "prompt_name"
    type = "S"
  }

  attribute {
    name = "version"
    type = "S"
  }

  # DELIBERATELY NO TTL, unlike post-metrics.
  #
  # This table is the rollback surface: analytics_engine auto-applies prompt
  # changes above 0.80 confidence, and recovering from a bad auto-update means
  # copying an older `content` back into Secrets Manager. Expiring rows would
  # quietly shorten how far back that recovery reaches.
  #
  # The 2-year retention decision was about posts and their engagement, which
  # is post-metrics. This table holds a few hundred rows of prompt text over
  # its whole life — bounded in practice, and cheap next to what it protects.
  # If it ever does need bounding, cap it by COUNT per prompt_name (keep the
  # last N versions), not by age: an old prompt that was working is exactly
  # what you want to roll back to.

  tags = {
    Name        = "${var.project_name}-prompt-versions"
    Environment = "production"
    ManagedBy   = "terraform"
  }
}

# ──────────────────────────────────────────────────────────
# IAM — Additional permissions for Lambda role
# (analytics: DynamoDB, CloudWatch PutMetricData, Glue)
# ──────────────────────────────────────────────────────────
resource "aws_iam_role_policy" "analytics_policy" {
  name = "${var.project_name}-analytics-policy"
  role = aws_iam_role.lambda_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "dynamodb:PutItem",
          "dynamodb:GetItem",
          "dynamodb:UpdateItem",
          "dynamodb:Query",
          "dynamodb:Scan"
        ]
        Resource = [
          aws_dynamodb_table.post_metrics.arn,
          "${aws_dynamodb_table.post_metrics.arn}/index/*",
          aws_dynamodb_table.prompt_versions.arn,
          "${aws_dynamodb_table.prompt_versions.arn}/index/*"
        ]
      },
      {
        Effect   = "Allow"
        Action   = ["cloudwatch:PutMetricData"]
        Resource = "*"
      },
      {
        Effect   = "Allow"
        Action   = ["glue:StartCrawler", "glue:GetCrawler"]
        Resource = "arn:aws:glue:${var.aws_region}:${data.aws_caller_identity.current.account_id}:crawler/${var.project_name}-*"
      },
      {
        Effect = "Allow"
        Action = [
          "secretsmanager:PutSecretValue"
        ]
        Resource = aws_secretsmanager_secret.credentials.arn
      }
    ]
  })
}

# ──────────────────────────────────────────────────────────
# IAM — Glue service role (crawler needs its own role)
# ──────────────────────────────────────────────────────────
resource "aws_iam_role" "glue_role" {
  name = "${var.project_name}-glue-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "glue.amazonaws.com" }
    }]
  })

  tags = {
    Name      = "${var.project_name}-glue-role"
    ManagedBy = "terraform"
  }
}

resource "aws_iam_role_policy_attachment" "glue_service" {
  role       = aws_iam_role.glue_role.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSGlueServiceRole"
}

resource "aws_iam_role_policy" "glue_s3_access" {
  name = "${var.project_name}-glue-s3"
  role = aws_iam_role.glue_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["s3:GetObject", "s3:ListBucket"]
      Resource = [aws_s3_bucket.results.arn, "${aws_s3_bucket.results.arn}/*"]
    }]
  })
}

# ──────────────────────────────────────────────────────────
# Glue — Database + Crawler for account metrics
# Athena can SQL-query S3 JSON files via Glue catalog
# ──────────────────────────────────────────────────────────
resource "aws_glue_catalog_database" "analytics" {
  name        = "${replace(var.project_name, "-", "_")}_analytics"
  description = "News AI Agent analytics data catalog"
}

resource "aws_glue_crawler" "account_metrics" {
  name          = "${var.project_name}-account-metrics"
  role          = aws_iam_role.glue_role.arn
  database_name = aws_glue_catalog_database.analytics.name
  description   = "Crawls daily account snapshots from S3"
  schedule      = "cron(0 3 ? * MON *)"  # Weekly Monday 03:00 UTC

  s3_target {
    path = "s3://${aws_s3_bucket.results.id}/metrics/account/"
  }

  s3_target {
    path = "s3://${aws_s3_bucket.results.id}/analytics/"
  }

  schema_change_policy {
    update_behavior = "UPDATE_IN_DATABASE"
    delete_behavior = "LOG"
  }

  tags = {
    Name      = "${var.project_name}-account-metrics-crawler"
    ManagedBy = "terraform"
  }
}

# ──────────────────────────────────────────────────────────
# Athena Workgroup — ad-hoc SQL on S3 data
# Results stored back in the same S3 bucket
# ──────────────────────────────────────────────────────────
resource "aws_athena_workgroup" "analytics" {
  name        = "${var.project_name}-analytics"
  description = "Analytics queries for news AI agent metrics"

  configuration {
    enforce_workgroup_configuration    = true
    publish_cloudwatch_metrics_enabled = true

    result_configuration {
      output_location = "s3://${aws_s3_bucket.results.id}/athena-results/"
    }
  }

  tags = {
    Name      = "${var.project_name}-athena-workgroup"
    ManagedBy = "terraform"
  }
}

# ──────────────────────────────────────────────────────────
# CloudWatch Dashboard
# Custom metrics pushed by analytics_engine + Logs Insights
# queries on structured JSON logs from Lambda Powertools
# ──────────────────────────────────────────────────────────
resource "aws_cloudwatch_dashboard" "analytics" {
  dashboard_name = "${var.project_name}-analytics"

  dashboard_body = templatefile("${path.module}/dashboard.json.tpl", {
    region       = var.aws_region
    project_name = var.project_name
  })
}

# ──────────────────────────────────────────────────────────
# Lambda — Metrics Collector (daily 02:00 AMS = 00:00 UTC)
# ──────────────────────────────────────────────────────────
resource "aws_lambda_function" "metrics_collector" {
  s3_bucket        = aws_s3_bucket.results.id
  s3_key           = aws_s3_object.lambda_zip.key
  function_name    = "${var.project_name}-metrics-collector"
  role             = aws_iam_role.lambda_role.arn
  handler          = "lambda_handler.handler_metrics_collector"
  runtime          = "python3.12"
  timeout          = 120
  memory_size      = 256

  source_code_hash = filebase64sha256("../../lambda_deployment.zip")

  environment {
    variables = {
      RESULTS_BUCKET      = aws_s3_bucket.results.id
      SECRET_NAME         = aws_secretsmanager_secret.credentials.name
      METRICS_TABLE       = aws_dynamodb_table.post_metrics.name
      SNS_ALERT_TOPIC_ARN = var.alert_email != "" ? aws_sns_topic.alerts[0].arn : ""
    }
  }

  tags = {
    Name        = "${var.project_name}-metrics-collector"
    Environment = "production"
    ManagedBy   = "terraform"
  }

  depends_on = [
    aws_iam_role_policy.analytics_policy,
    aws_dynamodb_table.post_metrics,
    aws_s3_object.lambda_zip,
  ]
}

resource "aws_cloudwatch_log_group" "metrics_collector_logs" {
  name              = "/aws/lambda/${var.project_name}-metrics-collector"
  retention_in_days = 14

  tags = {
    Name      = "${var.project_name}-metrics-collector-logs"
    ManagedBy = "terraform"
  }
}

resource "aws_cloudwatch_event_rule" "metrics_collector_schedule" {
  name                = "${var.project_name}-metrics-collector"
  description         = "Daily Instagram metrics collection — 02:00 Amsterdam (00:00 UTC)"
  schedule_expression = "cron(0 0 * * ? *)"

  tags = {
    Name      = "${var.project_name}-metrics-collector-schedule"
    ManagedBy = "terraform"
  }
}

resource "aws_cloudwatch_event_target" "metrics_collector_target" {
  rule      = aws_cloudwatch_event_rule.metrics_collector_schedule.name
  target_id = "metrics-collector-lambda"
  arn       = aws_lambda_function.metrics_collector.arn
}

resource "aws_lambda_permission" "allow_metrics_collector_eventbridge" {
  statement_id  = "AllowExecutionFromEventBridgeMetricsCollector"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.metrics_collector.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.metrics_collector_schedule.arn
}

# ──────────────────────────────────────────────────────────
# Lambda — Analytics Engine (weekly Sunday 22:00 AMS = 20:00 UTC)
# ──────────────────────────────────────────────────────────
resource "aws_lambda_function" "analytics_engine" {
  s3_bucket        = aws_s3_bucket.results.id
  s3_key           = aws_s3_object.lambda_zip.key
  function_name    = "${var.project_name}-analytics-engine"
  role             = aws_iam_role.lambda_role.arn
  handler          = "lambda_handler.handler_analytics_engine"
  runtime          = "python3.12"
  timeout          = 300  # 5 min — Claude Sonnet analysis can take ~30-60s
  memory_size      = 512

  source_code_hash = filebase64sha256("../../lambda_deployment.zip")

  environment {
    variables = {
      RESULTS_BUCKET        = aws_s3_bucket.results.id
      SECRET_NAME           = aws_secretsmanager_secret.credentials.name
      METRICS_TABLE         = aws_dynamodb_table.post_metrics.name
      PROMPT_VERSIONS_TABLE = aws_dynamodb_table.prompt_versions.name
      SNS_ALERT_TOPIC_ARN   = var.alert_email != "" ? aws_sns_topic.alerts[0].arn : ""
    }
  }

  tags = {
    Name        = "${var.project_name}-analytics-engine"
    Environment = "production"
    ManagedBy   = "terraform"
  }

  depends_on = [
    aws_iam_role_policy.analytics_policy,
    aws_dynamodb_table.post_metrics,
    aws_dynamodb_table.prompt_versions,
    aws_s3_object.lambda_zip,
  ]
}

resource "aws_cloudwatch_log_group" "analytics_engine_logs" {
  name              = "/aws/lambda/${var.project_name}-analytics-engine"
  retention_in_days = 30

  tags = {
    Name      = "${var.project_name}-analytics-engine-logs"
    ManagedBy = "terraform"
  }
}

resource "aws_cloudwatch_event_rule" "analytics_engine_schedule" {
  name                = "${var.project_name}-analytics-engine"
  description         = "Weekly analytics run — Sunday 22:00 Amsterdam (20:00 UTC)"
  schedule_expression = "cron(0 20 ? * SUN *)"

  tags = {
    Name      = "${var.project_name}-analytics-engine-schedule"
    ManagedBy = "terraform"
  }
}

resource "aws_cloudwatch_event_target" "analytics_engine_target" {
  rule      = aws_cloudwatch_event_rule.analytics_engine_schedule.name
  target_id = "analytics-engine-lambda"
  arn       = aws_lambda_function.analytics_engine.arn
}

resource "aws_lambda_permission" "allow_analytics_engine_eventbridge" {
  statement_id  = "AllowExecutionFromEventBridgeAnalyticsEngine"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.analytics_engine.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.analytics_engine_schedule.arn
}

# ──────────────────────────────────────────────────────────
# Lambda — Selection Reviewer (weekly Sunday 19:00 AMS = 17:00 UTC)
# Reviews the week's editorial picks and emails an AI growth/commercial review.
# Reuses the analytics IAM role (S3 read + DynamoDB read + SNS publish + Secrets).
# ──────────────────────────────────────────────────────────
resource "aws_lambda_function" "selection_reviewer" {
  s3_bucket        = aws_s3_bucket.results.id
  s3_key           = aws_s3_object.lambda_zip.key
  function_name    = "${var.project_name}-selection-reviewer"
  role             = aws_iam_role.lambda_role.arn
  handler          = "lambda_handler.handler_selection_review"
  runtime          = "python3.12"
  timeout          = 300  # 5 min — Claude Opus review can take ~30-90s
  memory_size      = 512

  source_code_hash = filebase64sha256("../../lambda_deployment.zip")

  environment {
    variables = {
      RESULTS_BUCKET        = aws_s3_bucket.results.id
      SECRET_NAME           = aws_secretsmanager_secret.credentials.name
      METRICS_TABLE         = aws_dynamodb_table.post_metrics.name
      SNS_ALERT_TOPIC_ARN   = var.alert_email != "" ? aws_sns_topic.alerts[0].arn : ""
    }
  }

  tags = {
    Name        = "${var.project_name}-selection-reviewer"
    Environment = "production"
    ManagedBy   = "terraform"
  }

  depends_on = [
    aws_iam_role_policy.analytics_policy,
    aws_dynamodb_table.post_metrics,
    aws_s3_object.lambda_zip,
  ]
}

resource "aws_cloudwatch_log_group" "selection_reviewer_logs" {
  name              = "/aws/lambda/${var.project_name}-selection-reviewer"
  retention_in_days = 30

  tags = {
    Name      = "${var.project_name}-selection-reviewer-logs"
    ManagedBy = "terraform"
  }
}

resource "aws_cloudwatch_event_rule" "selection_reviewer_schedule" {
  name                = "${var.project_name}-selection-reviewer"
  description         = "Weekly selection review — Sunday 19:00 Amsterdam (17:00 UTC)"
  schedule_expression = "cron(0 17 ? * SUN *)"

  tags = {
    Name      = "${var.project_name}-selection-reviewer-schedule"
    ManagedBy = "terraform"
  }
}

resource "aws_cloudwatch_event_target" "selection_reviewer_target" {
  rule      = aws_cloudwatch_event_rule.selection_reviewer_schedule.name
  target_id = "selection-reviewer-lambda"
  arn       = aws_lambda_function.selection_reviewer.arn
}

resource "aws_lambda_permission" "allow_selection_reviewer_eventbridge" {
  statement_id  = "AllowExecutionFromEventBridgeSelectionReviewer"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.selection_reviewer.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.selection_reviewer_schedule.arn
}

# ──────────────────────────────────────────────────────────
# Outputs
# ──────────────────────────────────────────────────────────
output "post_metrics_table_name" {
  description = "DynamoDB table for per-post engagement metrics"
  value       = aws_dynamodb_table.post_metrics.name
}

output "prompt_versions_table_name" {
  description = "DynamoDB table for prompt version history"
  value       = aws_dynamodb_table.prompt_versions.name
}

output "analytics_dashboard_url" {
  description = "CloudWatch analytics dashboard URL"
  value       = "https://${var.aws_region}.console.aws.amazon.com/cloudwatch/home?region=${var.aws_region}#dashboards:name=${var.project_name}-analytics"
}

output "athena_workgroup_name" {
  description = "Athena workgroup for ad-hoc analytics queries"
  value       = aws_athena_workgroup.analytics.name
}

output "glue_database_name" {
  description = "Glue Data Catalog database for analytics"
  value       = aws_glue_catalog_database.analytics.name
}
