# Use the official Python image as the base
FROM python:3.9-slim

# Set the working directory
WORKDIR /app

# Copy the application files
COPY . .

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Expose the port (optional, not required for a worker)
EXPOSE 8080

# Start the application
CMD ["python", "main.py"]
