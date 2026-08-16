import uuid

from langchain_anthropic import ChatAnthropic
from langchain_core.prompts import ChatPromptTemplate
from langchain_text_splitters import CharacterTextSplitter

from GENAI.vector_stores import VectorDocument, vector_store_factory

from .state import SupervisorState
from .utils import extract_text


async def run_pdf_agent(state: SupervisorState) -> dict:
    """Answer the query against the uploaded PDF via pgvector — chunks are embedded
    and persisted to Postgres (not held only in local process memory) and the
    search is scoped to this upload via a per-request tag."""
    text_splitter = CharacterTextSplitter(
        separator="\n",
        chunk_size=1500,
        chunk_overlap=20,
        length_function=len,
    )
    chunks = text_splitter.split_text(state["pdf_text"])

    upload_id = str(uuid.uuid4())
    vs = vector_store_factory("pgvector", "voyage")
    await vs.add_documents([
        VectorDocument(id=str(uuid.uuid4()), text=chunk, metadata={"upload_id": upload_id, "source": "pdf_agent"})
        for chunk in chunks
    ])

    docs = await vs.get_documents(query=state["query"], top_k=4, filters={"upload_id": upload_id})
    context = "\n\n".join(doc.content for doc in docs)

    llm = ChatAnthropic(model="claude-opus-5", max_tokens=1024)
    prompt = ChatPromptTemplate.from_template(
        "Use the following context to answer the question.\n\nContext:\n{context}\n\nQuestion: {question}\n\nAnswer:"
    )
    chain = prompt | llm
    response = await chain.ainvoke({"context": context, "question": state["query"]})
    return {"pdf_answer": extract_text(response.content)}
