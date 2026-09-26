# Build the base image first:
# docker build -f docker/python/Dockerfile -t senda-argus-python:0.1 .
FROM senda-argus-python:0.1

# Customer dependencies can be installed here.
# COPY requirements.txt /tmp/requirements.txt
# RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY agent.py /app/agent.py
CMD ["python", "/app/agent.py"]
