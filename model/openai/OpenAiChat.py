import os
import openai
from model.ChatBase import ChatBase
import logging
import json
import time

# save logging information to specified file

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
logger.addHandler(logging.FileHandler(f'openai-{time.localtime}.log'))

class OpenAiChat(ChatBase):
    '''
    The openai chatting class
    '''
    def __init__(self, api_key: str, temperature: float = 0, max_tokens: int = 400, top_p: int = 1, frequency_penalty=0, presence_penalty=0) -> None:
        '''
        Initialize the openai api client
        '''
        self.api_key = os.getenv("OPENAI_API_MY_SETUP") or api_key
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.top_p = top_p
        self.frequency_penalty = frequency_penalty
        self.presence_penalty = presence_penalty
        openai.api_key = self.api_key

    def retry_with_exponential_backoff(self, func, *args, **kwargs):
        '''
        Handle the openai exception and retry the request with exponential backoff
        '''
        try:
            return func(*args, **kwargs)
        except openai.error.APIError as e:
            logger.error(f"OpenAI API error occurred: {e}")
            logger.info("Retrying with exponential backoff...")
            return self.retry_with_exponential_backoff(func, *args, **kwargs)

    def get_response(self, messages: list, model: str):
        '''
        Get the response from the openai api
        '''
        try:
            response = self.retry_with_exponential_backoff(openai.ChatCompletion.create, model=model, messages=messages, temperature=self.temperature, max_tokens=self.max_tokens, top_p=self.top_p, frequency_penalty=self.frequency_penalty, presence_penalty=self.presence_penalty)
            return response.choices[0].text
        except openai.error.APIError as e:
            logger.error(f"OpenAI API error occurred: {e}")

    def extract_code(self, response: str):
        '''
        Extract the code from the response
        '''
        code = response.split('```')[1]
        return code

    def evaluation(self, response: str, code: str):
        '''
        Evaluate the response and code
        '''
        return response
    

# Example usage
if __name__ == "__main__":
    chat = OpenAiChat()
    response = chat.get_response(["please write a bash code to create the new docker container with the name 'my_container' and the image 'my_image', you don't need to explain it."], model="gpt-4o-mini")