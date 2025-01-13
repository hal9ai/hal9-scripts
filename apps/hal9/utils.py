import json
import os
import urllib.parse
import urllib.request
import requests
from typing import Literal, List, Dict, Any, Union, Optional
from clients import openai_client, azure_openai_client
from groq import Groq
from openai import AzureOpenAI, OpenAI
import fitz
from io import BytesIO
import pandas as pd
from concurrent.futures import ThreadPoolExecutor
import ast
import re

# Define the allowed client types.
ClientType = Literal["openai", "azure", "groq"]

def get_client(client_type: ClientType) -> Union[OpenAI, AzureOpenAI, Groq]:
    """
    Returns the appropriate client instance based on the given type.

    Parameters:
        client_type (ClientType): The type of client ("openai", "azure", "groq").

    Returns:
        Union[openai_client, azure_openai_client, Groq]: An instance of the selected client.
    
    Raises:
        ValueError: If the provided client type is not supported.
    """
    if client_type == "openai":
        return openai_client
    elif client_type == "azure":
        return azure_openai_client
    elif client_type == "groq":
        return Groq()
    else:
        raise ValueError(f"Unsupported client type: {client_type}")

def generate_response(
    client_type: ClientType,
    model: str,
    messages: List[Dict[str, Any]],
    tools: Optional[List] = None,
    tool_choice: Optional[str] = None,
    parallel_tool_calls: Optional[bool] = True,
    temperature: Optional[float] = None,
    seed: Optional[int] = None,
    top_p: Optional[float] = None,
    frequency_penalty: Optional[float] = None,
    max_completion_tokens: Optional[int] = None,
    n: int = 1
) -> Dict[str, Any]:
    """
    Generates a response using the appropriate client based on the specified type.

    Parameters:
        client_type (ClientType): The type of client ("openai", "azure", "groq").
        model (str): The model to use for generating the response.
        messages (List[Dict[str, Any]]): List of messages to provide as context.
        tools (Optional[List]): Available tools for the model. Default is None.
        tool_choice (Optional[str]): The selected tool to use. Default is None.
        temperature (Optional[float]): Controls randomness in the output (0 to 1). Default is None.
        seed (Optional[int]): Seed for reproducible randomness. Default is None.
        top_p (Optional[float]): Probability mass for nucleus sampling. Default is None.
        frequency_penalty (Optional[float]): Penalizes repetition. Default is None.
        max_completion_tokens (Optional[int]): Max tokens for the response. Default is None.
        n (int): Number of responses to generate. Default is 1.

    Returns:
        Dict[str, Any]: The response generated by the selected client.
    """
    # Get the appropriate client instance.
    client = get_client(client_type)

    # Prepare the payload dynamically.
    payload = {
        "model": model,
        "messages": messages,
        "tools": tools,
        "tool_choice": tool_choice,
        "temperature": temperature,
        "seed": seed,
        "top_p": top_p,
        "frequency_penalty": frequency_penalty,
        "max_tokens": max_completion_tokens,
        "n": n
    }

    if tools is not None:
        payload["parallel_tool_calls"] = parallel_tool_calls

    # Generate the response using the client's completion API.
    response = client.chat.completions.create(**payload)

    return response

def load_messages(file_path="./.storage/.messages.json") -> List[Dict[str, Any]]:
    """
    Loads messages from a JSON file located in the './.storage' directory.

    Returns:
        List[Dict[str, Any]]: A list of messages if the file exists and is valid.
    """
    if not os.path.exists(file_path):
        return []
    else :
        with open(file_path, "r", encoding="utf-8") as file:
            messages = json.load(file)

        return messages

def save_messages(messages: List[Dict[str, Any]], file_path="./.storage/.messages.json") -> None:
    """
    Saves messages to a JSON file located in the './.storage' directory.

    Args:
        messages (List[Dict[str, Any]]): A list of messages to be saved.
    """

    with open(file_path, "w", encoding="utf-8") as file:
        json.dump(messages, file, ensure_ascii=False, indent=4)

def insert_message(messages , role, content, tool_call_id=None):
    if tool_call_id:
        return None
    else:
        messages.append({"role": role, "content": content})
    return messages

def execute_function(model_response, functions):
    # Extract the message from the response.
    try:
        response_message = model_response.choices[0].message
    except (IndexError, AttributeError) as e:
        print(f"Error extracting message from model response: {e}")
        return

    # Access the tool calls (if any) from the message.
    tool_calls = getattr(response_message, 'tool_calls', None)

    if not tool_calls:
        print("No tool calls found.")
        return

    # Iterate over the tool calls and extract relevant information.
    for tool_call in tool_calls:
        function_name = tool_call.function.name
        try:
            arguments = ast.literal_eval(tool_call.function.arguments)
        except AttributeError as e:
            print(f"Error accessing arguments: {e}")
            continue
        # Convert arguments into a string format for logging or execution.
        args_str = ', '.join(f"{k}={repr(v)}" for k, v in arguments.items())

        # Add all the functions into the exec context
        context = {}
        for func in functions:
            context[func.__name__] = func
        
        # Prepare the code string to execute
        code_to_exec = f"result = {function_name}({args_str})"

        # Execute the code with exec(), but ensure proper error handling.
        try:
            exec(code_to_exec, context)
            return context['result']
        except Exception as e:
            print(f"Error executing function '{function_name}': {e}")
            raise

def stream_print(stream, show = True):
    content = ""
    for chunk in stream:
      if len(chunk.choices) > 0 and chunk.choices[0].delta.content is not None: 
        if show:
            print(chunk.choices[0].delta.content, end="")
        content += chunk.choices[0].delta.content
    return content

def insert_tool_message(messages, model_response, tool_result):
    tool_calls = model_response.choices[0].message.tool_calls

    if tool_calls:
      for tool_call in tool_calls:
        messages.append({
          "role": "assistant",
          "tool_calls": [{
            "id": tool_call.id,
            "type": "function",
            "function": {
              "arguments": tool_call.function.arguments,
              "name": tool_call.function.name,
            },
          }]
        })
        function_args = json.loads(tool_call.function.arguments, strict=False)

        tool_content = json.dumps({**function_args, "response": str(tool_result)})

        messages.append({
            "role": "tool",
            "content": tool_content,
            "tool_call_id": tool_call.id
        })

def is_url(prompt):
  result = urllib.parse.urlparse(prompt)
  return all([result.scheme, result.netloc])

def download_file(url):
    filename = url.split("/")[-1]
    modified_filename = f"./.storage/.{filename}"
    
    response = requests.get(url)
    
    if response.status_code == 200:
        with open(modified_filename, 'wb') as file:
            file.write(response.content)
    else:
        print(f"Failed to download the file. Status code: {response.status_code}")

def generate_embeddings(text, model, client_type):
    client = get_client(client_type)
    response = client.embeddings.create(
    input=text,
    model=model)

    return response.data[0].embedding

def split_text(text, n_words=300, overlap=0):
    """
    Splits a text into chunks of `n_words` words with an overlap of `overlap` words.

    Args:
        text (str): The input text to be split.
        n_words (int): Number of words per chunk.
        overlap (int): Number of overlapping words between consecutive chunks.

    Returns:
        list: A list of text chunks.
    """
    # Validate inputs
    if overlap >= n_words:
        raise ValueError("Overlap must be smaller than the number of words per chunk.")

    # Split the text into words
    words = text.split()
    chunks = []

    # Generate the chunks
    start = 0
    while start < len(words):
        end = start + n_words
        chunk = words[start:end]
        chunks.append(" ".join(chunk))
        
        # Move the start point forward, with overlap
        start += n_words - overlap

    return chunks

def process_chunk(chunk_info):
    chunk, page_num, model, client_type = chunk_info
    embedding = generate_embeddings(chunk, model=model, client_type=client_type)
    return {
        "text": chunk,
        "embedding": embedding,
        "page": page_num + 1  # Page numbers start from 1
    }

def generate_text_embeddings_parquet(url, model="text-embedding-3-small", client_type="azure", n_words=300, overlap=0, max_threads=8):
    # Download and read the PDF
    response = requests.get(url)
    pdf_document = fitz.open(stream=BytesIO(response.content))
    
    # Prepare chunk info for parallel processing
    chunk_info_list = []
    for page_num in range(len(pdf_document)):
        page = pdf_document[page_num]
        page_text = page.get_text()

        # Split the page text into chunks
        text_chunks = split_text(page_text, n_words=n_words, overlap=overlap)

        # Add chunk info to the list
        for chunk in text_chunks:
            chunk_info_list.append((chunk, page_num, model, client_type))

    pdf_document.close()

    # Process chunks in parallel
    rows = []
    with ThreadPoolExecutor(max_threads) as executor:
        for result in executor.map(process_chunk, chunk_info_list):
            rows.append(result)

    # Create the DataFrame
    df = pd.DataFrame(rows)

    # Add a global chunk ID column
    df['chunk_id'] = range(len(df))
    df['filename'] = '.' + url.split("/")[-1]

    # Save as Parquet
    df.to_parquet("./.storage/.text_files.parquet", engine="pyarrow", index=False)

def load_json_file(json_path):
    if os.path.exists(json_path):
        with open(json_path, 'r') as file:
            return json.load(file)
    return []

def extract_code_block(code: str, language: str) -> str:
    pattern = rf"```{language}\n(.*?)```"
    match = re.search(pattern, code, re.DOTALL)
    return match.group(1) if match else ""