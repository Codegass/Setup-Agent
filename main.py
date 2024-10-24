# from langchain_community.llms import Ollama

# llm = Ollama(model="llama3")

# print(llm.invoke("please write a bash code to create the new docker container with the name 'my_container' and the image 'my_image', you don't need to explain it."))

from model.openai.OpenAiChat import OpenAiChat

chat = OpenAiChat()
response = chat.get_response(["please write a bash code to create the new docker container with the name 'my_container' and the image 'my_image', you don't need to explain it."], model="gpt-4o-mini")
print(response.content)