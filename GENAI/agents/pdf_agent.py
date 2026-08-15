from langchain_anthropic import ChatAnthropic
from langchain_community.vectorstores.faiss import FAISS
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_text_splitters import CharacterTextSplitter

from GENAI.vector_stores import VoyageEmbeddings

from .state import SupervisorState


def run_pdf_agent(state: SupervisorState) -> dict:
    """Answer the query against the uploaded PDF using an in-memory FAISS RAG lookup."""
    text_splitter = CharacterTextSplitter(
        separator="\n",
        chunk_size=1500,
        chunk_overlap=20,
        length_function=len,
    )
    chunks = text_splitter.split_text(state["pdf_text"])

    embeddings = VoyageEmbeddings()
    knowledge_base = FAISS.from_texts(chunks, embeddings.embeddings)
    docs = knowledge_base.similarity_search(state["query"], k=4)
    context = "\n\n".join(doc.page_content for doc in docs)

    llm = ChatAnthropic(model="claude-opus-5", max_tokens=1024)
    prompt = ChatPromptTemplate.from_template(
        "Use the following context to answer the question.\n\nContext:\n{context}\n\nQuestion: {question}\n\nAnswer:"
    )
    chain = prompt | llm | StrOutputParser()
    answer = chain.invoke({"context": context, "question": state["query"]})
    return {"pdf_answer": answer}
