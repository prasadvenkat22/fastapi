import requests
import json

url = "http://localhost:8000/api/genai/query"

data = {
    "query": "What is the file about?",
    "top_k": 1,
    "embedding_provider": "voyage"
}

response = requests.post(url, json=data)

print(response.status_code)
print(response.text)
