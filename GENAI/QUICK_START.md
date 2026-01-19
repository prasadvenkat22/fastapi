# Quick Start Guide - RAG API for React Frontend

## TL;DR - For React Developers

### Installation
```bash
npm install axios
```

### Minimal React Example
```jsx
import { useState } from 'react';
import axios from 'axios';

function FileUpload() {
  const [file, setFile] = useState(null);
  const [result, setResult] = useState('');

  const handleSubmit = async (e) => {
    e.preventDefault();

    const formData = new FormData();
    formData.append('files', file);
    formData.append('query', 'Summarize this document');
    formData.append('username', 'current_user_id'); // Pass logged-in user

    const res = await axios.post(
      'http://localhost:8000/api/genai/query/upload',
      formData
    );

    setResult(res.data.results[0].content);
  };

  return (
    <form onSubmit={handleSubmit}>
      <input type="file" onChange={(e) => setFile(e.target.files[0])} />
      <button type="submit">Upload & Ask</button>
      <p>{result}</p>
    </form>
  );
}
```

---

## API Endpoint

```
POST http://localhost:8000/api/genai/query/upload
```

---

## Required Parameters

| Name | Type | Example | Notes |
|------|------|---------|-------|
| `files` | File | `document.pdf` | Required |
| `query` | string | `"What is the main topic?"` | Required |
| `username` | string | `"john_doe"` | **Auto-creates user in MongoDB** (defaults to `test_user`) |

---

## Response Format

```json
{
  "results": [
    {
      "content": "The answer to your question is...",
      "metadata": { "source": "rag_with_vector_search" }
    }
  ]
}
```

---

## Complete Working Examples

### 1. React (TypeScript)
```typescript
interface UploadResponse {
  results: Array<{
    content: string;
    metadata: Record<string, any>;
  }>;
}

const uploadDocument = async (
  file: File,
  query: string,
  username: string
): Promise<string> => {
  const formData = new FormData();
  formData.append('files', file);
  formData.append('query', query);
  formData.append('username', username);

  const { data } = await axios.post<UploadResponse>(
    'http://localhost:8000/api/genai/query/upload',
    formData
  );

  return data.results[0].content;
};
```

### 2. JavaScript (Fetch)
```javascript
async function askDocument(file, question, username) {
  const formData = new FormData();
  formData.append('files', file);
  formData.append('query', question);
  formData.append('username', username);

  const response = await fetch('http://localhost:8000/api/genai/query/upload', {
    method: 'POST',
    body: formData,
  });

  const data = await response.json();
  return data.results[0].content;
}
```

### 3. Python (for testing)
```python
import requests

response = requests.post(
    'http://localhost:8000/api/genai/query/upload',
    files={'files': open('document.pdf', 'rb')},
    data={
        'query': 'Summarize this document',
        'username': 'john_doe'
    }
)

answer = response.json()['results'][0]['content']
print(answer)
```

---

## Supported File Types

- ✅ PDF (`.pdf`)
- ✅ CSV (`.csv`)
- ✅ Text (`.txt`)
- ✅ Word (`.docx`)
- ✅ Images (`.png`, `.jpg`, `.jpeg`) - with OCR

---

## Multiple Files

```javascript
const formData = new FormData();

// Add multiple files
fileList.forEach(file => {
  formData.append('files', file);
});

formData.append('query', 'Compare these documents');
formData.append('username', 'john_doe');
```

---

## Error Handling

```javascript
try {
  const response = await axios.post(url, formData);
  console.log(response.data);
} catch (error) {
  if (error.response) {
    // API returned an error
    console.error('Error:', error.response.data.detail);
  } else {
    // Network or other error
    console.error('Error:', error.message);
  }
}
```

---

## Environment Setup

### 1. Create `.env.local` in React project
```env
REACT_APP_API_URL=http://localhost:8000
```

### 2. Use in code
```javascript
const API_URL = process.env.REACT_APP_API_URL;
```

---

## Testing Checklist

- [ ] Upload a PDF file
- [ ] Upload a CSV file
- [ ] Upload multiple files at once
- [ ] Pass username parameter
- [ ] Handle loading state
- [ ] Handle error responses
- [ ] Display results to user

---

## Common Issues & Fixes

### Issue: CORS Error
**Solution:** CORS is already enabled on backend, check browser console

### Issue: File too large
**Solution:** Default limit is 10MB. Contact backend team to increase

### Issue: Slow response
**Solution:** Normal for first request. LLM processing takes 3-10 seconds

### Issue: Empty response
**Solution:** Check if file was uploaded correctly and query is not empty

---

## Advanced Options (Optional)

```javascript
formData.append('temperature', '0.7');      // 0.0 = factual, 1.0 = creative
formData.append('max_tokens', '1000');      // Longer responses
formData.append('top_k', '5');              // More context from documents
formData.append('llm_model', 'gpt-4o-mini'); // Change model
```

---

## User Auto-Registration

**Important:** When you upload a file, the username is automatically registered in MongoDB at `/api/mongo/users/` if it doesn't exist yet.

- **Email**: Auto-generated as `{username}@genai.app`
- **Password**: Default `GenAI@2024`
- **No duplicates**: Existing users are not re-created

Check registered users:
```bash
curl http://localhost:8000/api/mongo/users/
```

---

## Production Checklist

Before deploying to production:

1. [ ] Change `REACT_APP_API_URL` to production URL
2. [ ] Implement user authentication (users are auto-created with default password)
3. [ ] Add file size validation on frontend
4. [ ] Add rate limiting
5. [ ] Restrict CORS to your domain
6. [ ] Handle API key rotation
7. [ ] Add loading indicators
8. [ ] Add retry logic for failed requests
9. [ ] Change default user password from `GenAI@2024` to secure random generation

---

## Need Help?

- **API Documentation:** See `API_DOCUMENTATION.md`
- **React Example:** See `react_example.jsx`
- **Backend Code:** `GENAI/router.py`

---

## Quick Test (No Code)

### Using cURL:
```bash
curl -X POST "http://localhost:8000/api/genai/query/upload" \
  -F "files=@document.pdf" \
  -F "query=What is this about?" \
  -F "username=test_user"
```

### Using Postman:
1. POST → `http://localhost:8000/api/genai/query/upload`
2. Body → form-data
3. Add `files` (File type)
4. Add `query` (Text type)
5. Add `username` (Text type)
6. Send
