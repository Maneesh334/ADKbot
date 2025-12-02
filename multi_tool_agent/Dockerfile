# Use an official Python runtime as a parent image
FROM python:3.11-slim

# Set the working directory in the container
WORKDIR /app

# Copy the requirements file into the container
COPY requirements.txt .

# Install any needed packages specified in requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application code into the container
COPY . .

# Cloud Run will set the PORT environment variable. 
# We'll expose a default port for development/testing.
# The `uvicorn.run` in your agent.py will use the PORT env var.
EXPOSE 8080 

# Run the uvicorn server, executing the agent.py file
CMD ["python", "agent.py"]