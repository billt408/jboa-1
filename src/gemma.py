import json, os, urllib.error, urllib.request

IGNORE_DIRS = {".venv", "venv", "__pycache__", ".git", "node_modules"}
code_bundle = []

for root, dirs, files in os.walk("/home/billt/jboa-1/src"):
    dirs[:] = [d for d in dirs if d not in IGNORE_DIRS]
    for name in sorted(files):
        if name.endswith((".py", ".sh", ".ini", ".md")) and name != "gemma.py":
            path = os.path.join(root, name)
            with open(path, "r", errors="ignore") as fp:
                code_bundle.append(f"--- FILE: {path} ---\n{fp.read()}")

initial_prompt = (
    "Here is my project source code. Acknowledge you have read it and summarize its purpose briefly:\n\n"
    + "\n\n".join(code_bundle)
)
print(f"Loaded {len(code_bundle)} files ({len(initial_prompt)} chars)...")

messages = [{"role": "user", "content": initial_prompt}]


def ask_gemma(msgs):
    payload = json.dumps(
        {
            "model": "gemma3:4b",
            "messages": msgs,
            "stream": False,
            "keep_alive": "30m",
            "options": {"num_ctx": 16384},
        }
    ).encode("utf-8")

    req = urllib.request.Request(
        "http://localhost:11434/api/chat",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req) as res:
        return json.loads(res.read())["message"]["content"]


try:
    reply = ask_gemma(messages)
    messages.append({"role": "assistant", "content": reply})
    print(f"\nGemma:\n{reply}\n")

    while True:
        user_q = input("Ask a follow-up (or 'exit'): ").strip()
        if user_q.lower() in {"exit", "quit", "/bye"}:
            break
        if not user_q:
            continue
        messages.append({"role": "user", "content": user_q})
        reply = ask_gemma(messages)
        messages.append({"role": "assistant", "content": reply})
        print(f"\nGemma:\n{reply}\n")

except urllib.error.HTTPError as e:
    print(f"HTTP {e.code}: {e.read().decode('utf-8', errors='ignore')}")
