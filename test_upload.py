import requests
import os

url = "http://localhost:8000/api/genai/query/upload"

# Create a test file
with open("test.txt", "w") as f:
    f.write("This is a test file for the RAG architecture. It contains some text that will be indexed in the vector store.")

with open("test.txt", "rb") as f:
    files = {"files": ("test.txt", f, "text/plain")}
    data = {
        "query": "What is this file about?",
        "username": "test_user",  # Added username parameter
        "vector_store_name": "pgvector",
        "llm_provider": "anthropic",
        "llm_model": "claude-opus-5",
    }
    response = requests.post(url, files=files, data=data)

print(f"Status Code: {response.status_code}")
print(f"Response: {response.json()}")

# Clean up the test file
os.remove("test.txt")
