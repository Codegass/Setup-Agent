import os
import openai
from model.ChatBase import ChatBase
import logging
import random
import time

# save logging information to specified file

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

file_handler = logging.FileHandler(f'openai-{time.time()}.log')
file_handler.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
file_handler.setFormatter(formatter)

logger.addHandler(file_handler)

class OpenAiChat(ChatBase):
    '''
    The openai chatting class
    '''
    def __init__(self, api_key: str = os.getenv("OPENAI_API_MY_SETUP"), system_prompt: str = "You are a helpful assistant.", max_chat_history: int = 10, max_retry = 10, base_delay: int = 1, temperature: float = 0, max_tokens: int = 400, top_p: int = 1, frequency_penalty=0, presence_penalty=0) -> None:
        '''
        Initialize the openai api client
        '''
        self.client = openai.OpenAI(api_key=api_key)
        self.max_chat_history = max_chat_history
        self.max_retry = max_retry
        self.base_delay = base_delay
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.top_p = top_p
        self.frequency_penalty = frequency_penalty
        self.presence_penalty = presence_penalty
        self.messages_queue = []
        self.system_prompt = system_prompt


    def retry_with_exponential_backoff(self, func, *args, retries=0, **kwargs):
        '''
        处理 OpenAI 异常并使用指数退避策略重试请求
        '''
        try:
            return func(*args, **kwargs)
        except openai.APIError as e:
            retries += 1
            if retries > self.max_retry:
                logger.error(f"reach max retry {self.max_retry}, last error: {e}")
                raise

            delay = (2 ** retries + random.random()) * self.base_delay
            logger.error(f"OpenAI API exception: {e}")
            logger.info(f"Now is the {retries} times retry, wait for {delay:.2f} sec...")
            time.sleep(delay)
            
            return self.retry_with_exponential_backoff(func, *args, retries=retries, **kwargs)

    def get_response(self, message: list, model: str):
        '''
        Get the response from the openai api
        '''
        self.structure_message(message)
        try:
            response = self.retry_with_exponential_backoff(
                self.client.chat.completions.create, 
                model=model, 
                messages=self.messages_queue, 
                temperature=self.temperature, 
                max_tokens=self.max_tokens, 
                top_p=self.top_p, 
                frequency_penalty=self.frequency_penalty, 
                presence_penalty=self.presence_penalty    
            )
            return response.choices[0].message
        except openai.APIError as e:
            logger.error(f"OpenAI API error occurred: {e}")
        except Exception as e:
            logger.error(f"Error occurred while getting OpenAI API response: {e}")
            raise

    def structure_message(self, message):
        '''
        Structure the message for the API with prompt and history messages
        '''
        if isinstance(message, str):
            message = [{"role": "user", "content": message}]
        elif isinstance(message, list):
            if all(isinstance(m, str) for m in message):
                message = [{"role": "user", "content": m} for m in message]
            elif not all(isinstance(m, dict) and "role" in m and "content" in m for m in message):
                raise ValueError("Invalid message format. Should be a string, list of strings, or list of properly formatted message objects.")

        self.messages_queue.extend(message)

        if len(self.messages_queue) > self.max_chat_history:
            # Remove oldest user messages, keep system message
            self.messages_queue = [msg for msg in self.messages_queue if msg["role"] == "system"] + \
                                self.messages_queue[-(self.max_chat_history-1):]

        # Ensure system prompt is always at the beginning of the queue
        if not self.messages_queue or self.messages_queue[0]["role"] != "system":
            self.messages_queue.insert(0, {"role": "system", "content": self.system_prompt})

        return self.messages_queue
        

    def set_system_prompt(self, prompt: str):
        '''
        Set the system prompt
        '''
        self.system_prompt = prompt
        # check if the system prompt is already in the messages queue
        if not any(message["role"] == "system" and message["content"] == prompt for message in self.messages_queue):
            self.messages_queue.append({"role": "system", "content": prompt})
        else:
            logger.info("System prompt already in the messages queue")
            # chage the system prompt in the messages queue
            self.messages_queue[0]["content"] = prompt          


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