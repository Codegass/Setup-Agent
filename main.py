from langchain_community.llms import Ollama

llm = Ollama(model="llama3")

print(llm.invoke("please write a bash code to create the new docker container with the name 'my_container' and the image 'my_image', you don't need to explain it."))