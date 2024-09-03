import os
import streamlit as st
import pandas as pd
from langchain_experimental.agents import create_pandas_dataframe_agent
from langchain.chains import LLMChain
from langchain_openai import OpenAI
from dotenv import load_dotenv
import streamlit as st
from PyPDF2 import PdfReader
from langchain.text_splitter import RecursiveCharacterTextSplitter,CharacterTextSplitter
from langchain_openai.embeddings import OpenAIEmbeddings
from langchain_community.vectorstores.faiss import FAISS
from langchain.chains.question_answering import load_qa_chain
from langchain_community.llms import OpenAI
from langchain_community.callbacks import get_openai_callback
from langchain.chains.combine_documents import create_stuff_documents_chain

load_dotenv()  # take environment variables from .env.
apikey=os.getenv("OPEN_API_KEY")
#llm = OpenAI(openai_api_key= apikey)
os.environ["OPEN_API_KEY"] = apikey
print(apikey)
OpenAI.api_key = apikey

# process text from pdf
def process_text(text):
  # split the text into chunks using langchain
  text_splitter = CharacterTextSplitter(
    separator="\n",
    chunk_size=1500,
    chunk_overlap=20,
    length_function=len
  )

  chunks = text_splitter.split_text(text)

  # convert the chunks of text into embeddings to form a knowledge base
  embeddings = OpenAIEmbeddings(openai_api_key=os.environ.get("OPEN_API_KEY"))
  knowledge_base = FAISS.from_texts(chunks, embeddings)
  return knowledge_base
def main():
    print("stert starlett")
    st.set_page_config(page_title="csv / pdf generative AI" ,layout="wide")
    st.header('Gen AI for RAG - Csv/Pdf Analytics')
    user_csv= st.file_uploader("upload your Csv file", type="csv")
    pdf = st.file_uploader("Upload your PDF File", type="pdf")
    if  user_csv:
        data= pd.read_csv(user_csv)
        st.write("Date Preview")
        st.dataframe(data.head())
        agent = create_pandas_dataframe_agent(OpenAI( temperature=0, max_tokens=100), data, verbose=True, model_name="GPT-4o",allow_dangerous_code=True)
        query = st.text_input("Enter a query")
        if st.button("Execure query"):
            answer = agent.invoke(query)
            st.write("Answer :  " )
            st.write(answer)
    if pdf is not None:
        pdf_reader = PdfReader(pdf)
    
    # store the pdf text in a var
        text = ""
        for page in pdf_reader.pages:
            text += page.extract_text()
    # create a knowledge base object
        knowledgeBase = process_text(text)
        query = st.text_input('Ask question to PDF...')
        cancel_button = st.button('Cancel')
        if cancel_button:
            st.stop()

        if query:
            docs = knowledgeBase.similarity_search(query)
            llm = OpenAI(openai_api_key=os.environ.get("OPEN_API_KEY"))
            chain = load_qa_chain(llm, chain_type="stuff")
            with get_openai_callback() as cost:
              response = chain.invoke(input={"question": query, "input_documents": docs})
              print(cost)
              st.write(response["output_text"])



if __name__ == "__main__":
    main()

