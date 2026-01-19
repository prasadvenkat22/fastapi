// React Component Example for File Upload and RAG Query
// This component demonstrates how to integrate with the FastAPI GENAI endpoint

import React, { useState } from 'react';
import axios from 'axios';

const DocumentUploadRAG = () => {
  const [files, setFiles] = useState([]);
  const [query, setQuery] = useState('');
  const [username, setUsername] = useState('');
  const [response, setResponse] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  const API_BASE_URL = process.env.REACT_APP_API_URL || 'http://localhost:8000';

  const handleFileChange = (e) => {
    setFiles(Array.from(e.target.files));
  };

  const handleSubmit = async (e) => {
    e.preventDefault();

    if (files.length === 0) {
      setError('Please select at least one file');
      return;
    }

    if (!query.trim()) {
      setError('Please enter a question');
      return;
    }

    setLoading(true);
    setError(null);
    setResponse(null);

    try {
      const formData = new FormData();

      // Add all files
      files.forEach((file) => {
        formData.append('files', file);
      });

      // Add required parameters
      formData.append('query', query);
      formData.append('username', username || 'test_user');

      // Optional parameters (using defaults from backend)
      // formData.append('vector_store_name', 'pgvector');
      // formData.append('embedding_provider', 'openai');
      // formData.append('llm_provider', 'openai');
      // formData.append('llm_model', 'gpt-4o-mini');
      // formData.append('temperature', '0.0');
      // formData.append('max_tokens', '800');
      // formData.append('top_k', '4');

      const result = await axios.post(
        `${API_BASE_URL}/api/genai/query/upload`,
        formData,
        {
          headers: {
            'Content-Type': 'multipart/form-data',
          },
        }
      );

      setResponse(result.data);
    } catch (err) {
      setError(err.response?.data?.detail || 'An error occurred');
      console.error('Upload error:', err);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="max-w-2xl mx-auto p-6">
      <h1 className="text-2xl font-bold mb-6">Document Upload & RAG Query</h1>

      <form onSubmit={handleSubmit} className="space-y-4">
        {/* Username Input */}
        <div>
          <label className="block text-sm font-medium mb-2">
            Username (optional)
          </label>
          <input
            type="text"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            placeholder="test_user"
            className="w-full px-3 py-2 border rounded-md"
          />
        </div>

        {/* File Upload */}
        <div>
          <label className="block text-sm font-medium mb-2">
            Upload Files (PDF, CSV, TXT, DOCX, Images)
          </label>
          <input
            type="file"
            multiple
            onChange={handleFileChange}
            accept=".pdf,.csv,.txt,.docx,.png,.jpg,.jpeg"
            className="w-full px-3 py-2 border rounded-md"
          />
          {files.length > 0 && (
            <p className="text-sm text-gray-600 mt-1">
              {files.length} file(s) selected
            </p>
          )}
        </div>

        {/* Query Input */}
        <div>
          <label className="block text-sm font-medium mb-2">
            Question
          </label>
          <textarea
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="What would you like to know about the documents?"
            rows={3}
            className="w-full px-3 py-2 border rounded-md"
          />
        </div>

        {/* Submit Button */}
        <button
          type="submit"
          disabled={loading}
          className="w-full bg-blue-600 text-white py-2 px-4 rounded-md hover:bg-blue-700 disabled:bg-gray-400"
        >
          {loading ? 'Processing...' : 'Upload & Query'}
        </button>
      </form>

      {/* Error Message */}
      {error && (
        <div className="mt-4 p-4 bg-red-100 border border-red-400 text-red-700 rounded">
          {error}
        </div>
      )}

      {/* Response */}
      {response && (
        <div className="mt-6 p-4 bg-green-50 border border-green-200 rounded">
          <h2 className="text-lg font-semibold mb-2">Response:</h2>
          {response.results && response.results.length > 0 && (
            <div className="space-y-2">
              {response.results.map((result, index) => (
                <div key={index} className="p-3 bg-white rounded border">
                  <p className="whitespace-pre-wrap">{result.content}</p>
                  {result.score && (
                    <p className="text-sm text-gray-500 mt-2">
                      Similarity Score: {result.score.toFixed(4)}
                    </p>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
};

export default DocumentUploadRAG;


// ============================================
// Alternative: Fetch API Example (No Axios)
// ============================================

export const uploadDocumentWithFetch = async (files, query, username = 'test_user') => {
  const formData = new FormData();

  files.forEach((file) => {
    formData.append('files', file);
  });

  formData.append('query', query);
  formData.append('username', username);

  const response = await fetch('http://localhost:8000/api/genai/query/upload', {
    method: 'POST',
    body: formData,
  });

  if (!response.ok) {
    const error = await response.json();
    throw new Error(error.detail || 'Upload failed');
  }

  return response.json();
};


// ============================================
// Usage Example
// ============================================

/*
import DocumentUploadRAG from './components/DocumentUploadRAG';

function App() {
  return (
    <div className="App">
      <DocumentUploadRAG />
    </div>
  );
}

export default App;
*/
