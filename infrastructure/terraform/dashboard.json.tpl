{
  "widgets": [
    {
      "type": "metric",
      "x": 0, "y": 0, "width": 12, "height": 6,
      "properties": {
        "title": "Follower Count (weekly)",
        "metrics": [["NewsAIAgent", "FollowerCount"]],
        "period": 604800,
        "stat": "Maximum",
        "view": "timeSeries",
        "region": "${region}"
      }
    },
    {
      "type": "metric",
      "x": 12, "y": 0, "width": 12, "height": 6,
      "properties": {
        "title": "Posts Analyzed (weekly)",
        "metrics": [["NewsAIAgent", "PostsAnalyzed"]],
        "period": 604800,
        "stat": "Maximum",
        "view": "timeSeries",
        "region": "${region}"
      }
    },
    {
      "type": "log",
      "x": 0, "y": 6, "width": 8, "height": 6,
      "properties": {
        "title": "Best Posting Hours (Reach Amplification)",
        "region": "${region}",
        "query": "SOURCE '/aws/lambda/${project_name}-metrics-collector' | filter event = \"post_metrics_updated\" | stats avg(reach_amplification) as avg_ra, avg(save_rate) as avg_sr, count(*) as posts by hour_published | sort avg_ra desc | limit 24",
        "view": "table"
      }
    },
    {
      "type": "log",
      "x": 8, "y": 6, "width": 8, "height": 6,
      "properties": {
        "title": "Growth by Post Type",
        "region": "${region}",
        "query": "SOURCE '/aws/lambda/${project_name}-metrics-collector' | filter event = \"post_metrics_updated\" | stats avg(reach_amplification) as avg_ra, avg(save_rate) as avg_sr, avg(avg_watch_time) as avg_wt, count(*) as posts by post_type | sort avg_ra desc",
        "view": "table"
      }
    },
    {
      "type": "log",
      "x": 16, "y": 6, "width": 8, "height": 6,
      "properties": {
        "title": "Top Topics (Reach Amplification)",
        "region": "${region}",
        "query": "SOURCE '/aws/lambda/${project_name}-metrics-collector' | filter event = \"post_metrics_updated\" | stats avg(reach_amplification) as avg_ra, avg(save_rate) as avg_sr, count(*) as posts by topic | sort avg_ra desc | limit 10",
        "view": "table"
      }
    },
    {
      "type": "metric",
      "x": 0, "y": 12, "width": 12, "height": 4,
      "properties": {
        "title": "Lambda Errors",
        "metrics": [
          ["AWS/Lambda", "Errors", "FunctionName", "${project_name}"],
          ["AWS/Lambda", "Errors", "FunctionName", "${project_name}-metrics-collector"],
          ["AWS/Lambda", "Errors", "FunctionName", "${project_name}-analytics-engine"]
        ],
        "stat": "Sum",
        "period": 300,
        "view": "timeSeries",
        "region": "${region}"
      }
    },
    {
      "type": "metric",
      "x": 12, "y": 12, "width": 12, "height": 4,
      "properties": {
        "title": "Avg Reach Amplification",
        "metrics": [["NewsAIAgent", "AverageNormalizedEngagementRate"]],
        "period": 604800,
        "stat": "Average",
        "view": "timeSeries",
        "region": "${region}"
      }
    }
  ]
}
