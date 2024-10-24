import os
from anthropic import Anthropic
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
file_handler = logging.FileHandler(f'./log/claude-{current_time}.log')
file_handler.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
file_handler.setFormatter(formatter)

logger.addHandler(file_handler)

class ClaudeChat(ChatBase):
    '''
    The Claude chatting class
    '''
    def __init__(self, api_key: str = os.getenv("CLAUDE_API_MY_SETUP"), system_prompt: str = "You are a helpful assistant.", max_chat_history: int = 10, max_retry: int = 10, base_delay: int = 1) -> None:
        '''
        Initialize the Claude chat
        '''
        self.client = Anthropic(api_key=api_key)
        self.max_chat_history = max_chat_history
        self.max_retry = max_retry
        self.base_delay = base_delay
        self.messages_queue = []
        self.system_prompt = system_prompt

    def retry_with_exponential_backoff(self, func, *args, retries=0, **kwargs):
        '''
        Handle Claude exceptions and retry the request with exponential backoff
        '''
        try:
            return func(*args, **kwargs)
        except Exception as e:
            retries += 1
            if retries > self.max_retry:
                logger.error(f"Reached max retry {self.max_retry}, last error: {e}")
                raise

            delay = (2 ** retries + random.random()) * self.base_delay
            logger.error(f"Claude API exception: {e}")
            logger.info(f"Now is the {retries} times retry, wait for {delay:.2f} sec...")
            time.sleep(delay)
            
            return self.retry_with_exponential_backoff(func, *args, retries=retries, **kwargs)

    def get_response(self, message: list, model: str = "claude-3-5-sonnet-latest", max_tokens: int = 2000):
        '''
        Get the response from the Claude API
        '''
        self.structure_message(message)
        try:
            response = self.retry_with_exponential_backoff(
                self.client.messages.create,
                model=model,
                max_tokens=max_tokens,
                messages=self.messages_queue,
                system=self.system_prompt
            )
            assistant_response = response.content[0].text
            # Update the last assistant message in the queue
            if self.messages_queue and self.messages_queue[-1]['role'] == 'assistant':
                self.messages_queue[-1]['content'] = assistant_response
            return assistant_response
        except Exception as e:
            logger.error(f"Error occurred while getting Claude API response: {e}")
            raise
#TODO: Implement the structure_message method, need add the assistant message to the message queue
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

        # Add new messages to the queue
        for msg in message:
            self.messages_queue.append(msg)
            # If the message is from the user, add a placeholder for the assistant's response
            if msg['role'] == 'user':
                self.messages_queue.append({"role": "assistant", "content": ""})

        # Trim the message queue to maintain max_chat_history
        if len(self.messages_queue) > self.max_chat_history:
            # Remove oldest messages, keeping an even number of messages (user + assistant pairs)
            num_to_remove = len(self.messages_queue) - self.max_chat_history
            if num_to_remove % 2 != 0:
                num_to_remove += 1
            self.messages_queue = self.messages_queue[num_to_remove:]

        return self.messages_queue

    def set_system_prompt(self, prompt: str):
        '''
        Set the system prompt
        '''
        self.system_prompt = prompt

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