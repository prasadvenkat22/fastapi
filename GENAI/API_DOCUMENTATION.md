# GENAI RAG API Documentation

## Overview
This API provides RAG (Retrieval-Augmented Generation) capabilities with document upload, vector storage, and intelligent querying using OpenAI LLMs and PostgreSQL pgvector.

---

## Base URL
```
http://localhost:8000/api/genai
```

---

## Endpoints

### 1. Upload & Query Documents
**Endpoint:** `POST /api/genai/query/upload`

Upload documents, store them in the vector database, and ask questions about them.

#### Supported File Types
- **PDF** (.pdf)
- **CSV** (.csv)
- **Text** (.txt)
- **Word** (.docx)
- **Images** (.png, .jpg, .jpeg, .tiff, .bmp) - with OCR

#### Request Parameters

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `files` | File[] | Yes | - | One or more files to upload |
| `query` | string | Yes | - | Question to ask about the documents |
| `username` | string | No | `test_user` | User who uploaded the file |
| `vector_store_name` | string | No | `pgvector` | Vector store (`pgvector` or `faiss`) |
| `embedding_provider` | string | No | `openai` | Embedding provider |
| `llm_provider` | string | No | `openai` | LLM provider |
| `llm_model` | string | No | `gpt-4o-mini` | LLM model to use |
| `temperature` | float | No | `0.0` | LLM temperature (0.0-1.0) |
| `max_tokens` | int | No | `800` | Max tokens in response |
| `top_k` | int | No | `4` | Number of similar documents to retrieve |

#### Example Request (cURL)
```bash
curl -X POST "http://localhost:8000/api/genai/query/upload" \
  -F "files=@document.pdf" \
  -F "query=What is the main topic of this document?" \
  -F "username=john_doe"
```

#### Example Request (JavaScript/React)
```javascript
const formData = new FormData();
formData.append('files', fileInput.files[0]);
formData.append('query', 'What is the main topic?');
formData.append('username', 'john_doe');

const response = await fetch('http://localhost:8000/api/genai/query/upload', {
  method: 'POST',
  body: formData,
});

const data = await response.json();
console.log(data.results);
```

#### Example Response
```json
{
  "results": [
    {
      "id": null,
      "content": "Based on the document, the main topic is...",
      "metadata": {
        "source": "rag_with_vector_search"
      },
      "score": null
    }
  ]
}
```

---

### 2. Query Vector Store
**Endpoint:** `POST /api/genai/query`

Query the vector store without uploading new documents.

#### Request Body
```json
{
  "query": "What are the company's policies?",
  "top_k": 4,
  "filters": {},
  "embedding_provider": "openai"
}
```

#### Example Request
```bash
curl -X POST "http://localhost:8000/api/genai/query" \
  -H "Content-Type: application/json" \
  -d '{
    "query": "What are the company policies?",
    "top_k": 4
  }'
```

#### Response
```json
{
  "results": [
    {
      "id": "doc-123",
      "content": "Document content...",
      "metadata": {
        "filename": "policies.pdf",
        "username": "john_doe"
      },
      "score": 0.8542
    }
  ]
}
```

---

### 3. Direct LLM Query
**Endpoint:** `POST /api/genai/llm`

Send a direct query to the LLM without RAG.

#### Request Body
```json
{
  "prompt": "Explain quantum computing in simple terms",
  "llm_provider": "openai",
  "llm_model": "gpt-4o-mini",
  "temperature": 0.7,
  "max_tokens": 500
}
```

#### Response
```json
{
  "content": "Quantum computing is...",
  "metadata": {
    "source": "openai"
  }
}
```

---

## Document Metadata

Each uploaded document is stored with the following metadata:

```json
{
  "filename": "document.pdf",
  "username": "john_doe",
  "uploaded_at": "2026-01-19T02:16:56.427154",
  "file_size": 1024000
}
```

You can filter documents by metadata using the `filters` parameter in queries.

---

## Vector Storage

### PostgreSQL + pgvector (Default)
- **Extension:** pgvector v0.8.1
- **Embedding Dimensions:** 1536 (OpenAI)
- **Similarity Metric:** Cosine distance
- **Table:** `documents` with columns:
  - `id` (TEXT PRIMARY KEY)
  - `text` (TEXT)
  - `metadata` (JSONB)
  - `embedding` (VECTOR(1536))

### Query Documents by User
```sql
SELECT * FROM documents
WHERE metadata->>'username' = 'john_doe';
```

### FAISS (Optional)
Set `vector_store_name=faiss` for local file-based storage.

---

## Error Handling

### Common Errors

#### 400 Bad Request
```json
{
  "detail": "OpenAI API error: Invalid API key"
}
```

#### 500 Internal Server Error
```json
{
  "detail": "Error during RAG operation: Database connection failed"
}
```

---

## React Integration

### Installation
```bash
npm install axios
```

### Complete Example Component
See `react_example.jsx` for a full React component with:
- File upload UI
- Multiple file support
- Loading states
- Error handling
- Response display

---

## Environment Variables

Required environment variables in `.env`:

```env
DATABASE_URL=postgresql://postgres:password@pgdb:5432/postgres
OPENAI_API_KEY=sk-...
GENAI_VECTORSTORE_PATH=local_vectorstore/db_faiss  # For FAISS
```

---

## Performance Considerations

1. **File Size Limits:** Default FastAPI limit is 10MB. Adjust in main.py if needed.
2. **Batch Processing:** Upload multiple files in a single request for efficiency.
3. **Top-K Selection:** Higher `top_k` values provide more context but slower responses.
4. **Temperature:** Lower values (0.0-0.3) for factual, higher (0.7-1.0) for creative.

---

## Testing

### Using Python
```python
import requests

files = {'files': open('document.pdf', 'rb')}
data = {
    'query': 'Summarize this document',
    'username': 'test_user'
}

response = requests.post(
    'http://localhost:8000/api/genai/query/upload',
    files=files,
    data=data
)

print(response.json())
```

### Using Postman
1. Set method to POST
2. URL: `http://localhost:8000/api/genai/query/upload`
3. Body → form-data
4. Add key `files` (type: File)
5. Add key `query` (type: Text)
6. Add key `username` (type: Text)
7. Click Send

---

## Security Notes

1. **CORS:** Currently allows all origins (`*`). Restrict in production.
2. **Authentication:** Add user authentication before production deployment.
3. **Rate Limiting:** Implement rate limiting for production use.
4. **File Validation:** Validates file types and sanitizes inputs.
5. **API Keys:** Store OpenAI keys securely in environment variables.

---

## Support & Troubleshooting

### Common Issues

**Issue:** "OpenAI API 400 Error"
- **Solution:** Check if `OPENAI_API_KEY` is valid and not expired

**Issue:** "Database connection error"
- **Solution:** Ensure PostgreSQL container is running: `docker-compose up -d`

**Issue:** "PDF text extraction failed"
- **Solution:** Ensure `pypdf` package is installed in container

**Issue:** "No results found"
- **Solution:** Check if documents were successfully uploaded to vector store

---

## Changelog

### v1.1.0
- Added `username` parameter for tracking uploads
- Added metadata tracking (upload time, file size)
- Changed default vector store to pgvector
- Enhanced error messages with details

### v1.0.0
- Initial release with RAG capabilities
- Support for PDF, CSV, TXT, DOCX, images
- PostgreSQL pgvector and FAISS support
