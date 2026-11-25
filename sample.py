from llama_cpp import Llama
import os

ONNX_PATH = r"""%USERPROFILE%\.cache\huggingface\hub\models--bartowski--microsoft_Phi-4-mini-instruct-GGUF\snapshots\7ff82c2aaa4dde30121698a973765f39be5288c0\microsoft_Phi-4-mini-instruct-Q4_K_M.gguf"""

MODEL_PATH = os.path.expandvars(ONNX_PATH)

print(MODEL_PATH)

print("Loading model...")
llm = Llama(
    model_path=MODEL_PATH,
    n_ctx=4096,
    n_threads=8,
    verbose=False
)

prompt = """
		Explain what a Python generator is and provide
		a simple example.
	 """

print("Generating...\n")

response = llm.create_chat_completion(
    messages=[
        { "role": "system", "content": "You are a helpful AI assistant." },
        { "role": "user","content": prompt }
    ],
    temperature=0.7,
    max_tokens=512
)

answer = response["choices"][0]["message"]["content"]

print("=" * 80)
print(answer)
print("=" * 80)