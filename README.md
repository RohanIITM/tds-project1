# AI Web Developer Agent

[![Python Version](https://img.shields.io/badge/Python-3.12-blue.svg)](https://www.python.org/downloads/release/python-3120/)
[![Docker](https://img.shields.io/badge/Docker-20.10.7-blue.svg)](https://www.docker.com/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.111.0-green.svg)](https://fastapi.tiangolo.com/)

This project is an AI-powered web developer agent. It receives a task, generates a single-page web application using an AI model, and deploys it to GitHub Pages.

## Getting Started

### Prerequisites

* Docker and Docker Compose
* A `.env` file with the necessary environment variables.

### Configuration

Create a `.env` file in the root of the project with the following variables:

```
secret=your_secret_key
github_token=your_github_token
openai_api_key=your_openai_api_key
openai_base_url=your_openai_base_url
```

### Running the application

To run the application, use the following command:

```bash
docker-compose up --build
```

The API will be available at `http://localhost:8000`.