import time
import ollama

MODEL = "llama3.1:latest"

client = ollama.Client(
    host="http://host.docker.internal:11434"
)

print("Senda Agent Runtime started", flush=True)
print(f"Using Ollama model: {MODEL}", flush=True)

response = client.chat(
    model=MODEL,
    messages=[
        {
            "role": "user",
            "content": "Senda Agent StudioのHook動作確認です。OKと短く返してください。"
        }
    ],
)

print("Ollama response:", response["message"]["content"], flush=True)

while True:
    time.sleep(30)
