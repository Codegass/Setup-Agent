from abc import ABC, abstractmethod

class ChatBase(ABC):
    '''
    The base chatting class for openai, groq, claude and gemini api.
    '''

    #### abstract method ####

    @abstractmethod
    def __init__(self, api_key: str, temperature: float = 0, max_tokens: int = 400, top_p: int = 1, frequency_penalty=0, presence_penalty=0) -> None:
        '''
        Initialize the api client
        '''
        pass


    @abstractmethod
    def retry_with_exponential_backoff(self, func, *args, **kwargs):
        '''
        Handle the api exception and retry the request with exponential backoff
        '''
        pass

    @abstractmethod
    def get_response(self, messages: list, model: str):
        '''
        Get the response from the api
        '''
        pass

    @abstractmethod
    def extract_code(self, response: str):
        '''
        Extract the code from the response
        '''
        pass

    @abstractmethod
    def evaluation(self, response: str, code: str):
        '''
        Evaluate the response and code
        '''
        pass


    

