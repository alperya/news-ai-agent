FROM public.ecr.aws/lambda/python:3.11 as builder

RUN yum install -y gcc gcc-c++ make

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt -t /opt/python

FROM public.ecr.aws/lambda/python:3.11

COPY --from=builder /opt/python /opt/python

COPY news_scraper.py ${LAMBDA_TASK_ROOT}/
COPY ai_agent.py ${LAMBDA_TASK_ROOT}/
COPY social_publisher.py ${LAMBDA_TASK_ROOT}/
COPY main.py ${LAMBDA_TASK_ROOT}/
COPY lambda_handler.py ${LAMBDA_TASK_ROOT}/

ENV PYTHONPATH=/opt/python:${LAMBDA_TASK_ROOT}

CMD ["lambda_handler.scheduled_handler"]
