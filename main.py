# from langchain_community.llms import Ollama

# llm = Ollama(model="llama3")

# print(llm.invoke("please write a bash code to create the new docker container with the name 'my_container' and the image 'my_image', you don't need to explain it."))

from model.chat import Chat

chat = Chat(service_provider="ollama")
res = chat.get_response(
    ["please write a bash code to create the new docker container with the name 'my_container' and the image 'my_image', you don't need to explain it."], 
    model="llama3.1"
    )
print(res)

chat = Chat(service_provider="groq")
res = chat.get_response(
    ["please write a bash code to create the new docker container with the name 'my_container' and the image 'my_image', you don't need to explain it."], 
    model="llama3-groq-8b-8192-tool-use-preview"
    )
print(res)

chat = Chat(service_provider="openai")
res = chat.get_response(
    ["please write a bash code to create the new docker container with the name 'my_container' and the image 'my_image', you don't need to explain it."], 
    model="gpt-4o-mini"
    )
print(res)

chat = Chat(service_provider="claude")
res = chat.get_response(
    ["please write a bash code to create the new docker container with the name 'my_container' and the image 'my_image', you don't need to explain it."], 
    model="claude-3-5-sonnet-latest"
    )
print(res)