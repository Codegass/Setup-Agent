# Setup-Agent
LLM Powered open source project setup agent

## idea

This tool main interface is a CLI tool, and it will use the LLM to help the user to setup the project in docker containers.

the project is managed by uv, a python package manager.

the project consist of three parts:
config: define the model provider(right now we consider of Openai, Anthropic, and ollama), logger, and the project config.
agent: should have a react engine to observe the situation and then use the tool, then observe again and plan for next step, this will need the agent to execute multiple step.
docker_orch: orchastration of the docker image for the agent. all the agent should only working in the dockers, they will have ability to communicate with each other. And the orchestration will be able to handle the docker image build and manage the lifecycle. 

our task here is to setup the project in docker, so the agent only working inside it, but when the task is done, user should be able to access the result and test, and maybe ask further questions of the project. from some point of view this is the docker is more like a artifact that the agent create with the valuebale results.

My image of the interaction process is like this:

in the cli interface the user will tell the agent which project he wants to setup, and the agent will use the docker_orch to build the docker image, and then run the agent in the docker container. agent will setup the project inside the docker container, and when the task is done, the agent will exit the docker container and the user will be able to access the result and test, and maybe ask further questions of the project. and based on user's request, the agent will go back again to the docker container and continue the task. so this will need the agent be able to manage the lifecycle of the docker container and maintain a list that the docker are created by it. The Agent execution style will be ReAct, and it will use multiple tools to achieve the task. there are two important things to consider: 1. how does the agent to manage the context of the docker container and the project so when it get back it can start the work right way. 2. how to design the tools that will be used by the agent to achieve the setup task.