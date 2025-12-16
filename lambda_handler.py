"""AWS Lambda Handler"""
import json
import logging
import boto3
from main import NewsAIPipeline

logger = logging.getLogger()
logger.setLevel(logging.INFO)

def lambda_handler(event, context):
    try:
        config = {
            'output_dir': '/tmp/output',
            'max_articles_per_source': 2,
            'max_posts': event.get('max_posts', 5),
            'dry_run': event.get('dry_run', True)
        }
        
        pipeline = NewsAIPipeline(config)
        results = pipeline.run()
        
        return {
            'statusCode': 200,
            'body': json.dumps({'message': 'Success', 'results': results})
        }
    except Exception as e:
        logger.error(f"Failed: {str(e)}")
        return {'statusCode': 500, 'body': json.dumps({'error': str(e)})}
