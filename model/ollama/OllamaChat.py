import os
import ollama
from model.ChatBase import ChatBase
import logging
import random
from datetime import datetime
import time

# Save logging information to specified file
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Check if the log folder exists
if not os.path.exists('log'):
    os.makedirs('log')

current_time = datetime.now().strftime("%Y%m%d%H%M%S")
file_handler = logging.FileHandler(f'./log/ollama-{current_time}.log')
file_handler.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
file_handler.setFormatter(formatter)

logger.addHandler(file_handler)

class OllamaChat(ChatBase):
    '''
    The Ollama chatting class
    '''
    def __init__(self, system_prompt: str = "You are a helpful assistant.", max_chat_history: int = 10, max_retry: int = 10, base_delay: int = 1) -> None:
        '''
        Initialize the Ollama chat
        '''
        self.max_chat_history = max_chat_history
        self.max_retry = max_retry
        self.base_delay = base_delay
        self.messages_queue = []
        self.system_prompt = system_prompt

    def retry_with_exponential_backoff(self, func, *args, retries=0, **kwargs):
        '''
        Handle Ollama exceptions and retry the request with exponential backoff
        '''
        try:
            return func(*args, **kwargs)
        except Exception as e:
            retries += 1
            if retries > self.max_retry:
                logger.error(f"Reached max retry {self.max_retry}, last error: {e}")
                raise

            delay = (2 ** retries + random.random()) * self.base_delay
            logger.error(f"Ollama API exception: {e}")
            logger.info(f"Now is the {retries} times retry, wait for {delay:.2f} sec...")
            time.sleep(delay)
            
            return self.retry_with_exponential_backoff(func, *args, retries=retries, **kwargs)

    def get_response(self, message: list, model: str):
        '''
        Get the response from the Ollama API
        '''
        self.structure_message(message)
        try:
            response = self.retry_with_exponential_backoff(
                ollama.chat,
                model=model,
                messages=self.messages_queue
            )
            return response['message']['content']
        except Exception as e:
            logger.error(f"Error occurred while getting Ollama API response: {e}")
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
        # Check if the system prompt is already in the messages queue
        if not any(message["role"] == "system" and message["content"] == prompt for message in self.messages_queue):
            self.messages_queue.insert(0, {"role": "system", "content": prompt})
        else:
            logger.info("System prompt already in the messages queue")
            # Change the system prompt in the messages queue
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