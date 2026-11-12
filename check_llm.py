"""Check the LLM settings in .env: sends one tiny request to each configured model.

Run:  python check_llm.py
"""
from recovery import config


def main():
    print(f"Provider: {config.LLM_PROVIDER}")
    if config.LLM_PROVIDER != "gemini":
        print("Not Gemini: nothing to check.")
        return
    if not config.GEMINI_API_KEY:
        raise SystemExit("GEMINI_API_KEY is missing from .env")
    from google import genai
    client = genai.Client(api_key=config.GEMINI_API_KEY)
    ok = 0
    for label, model in (("LLM_MODEL", config.LLM_MODEL), ("LLM_FALLBACK_MODEL", config.LLM_FALLBACK_MODEL)):
        if not model:
            continue
        try:
            text = client.models.generate_content(model=model, contents="Reply with just: OK").text.strip()
            print(f"  OK      {label}={model}  ->  {text}")
            ok += 1
        except Exception as e:
            print(f"  FAILED  {label}={model}  ->  {str(e)[:100]}")
    print("\nLLM ready." if ok else "\nNo model works: check the key and model names (python find_model.py).")


if __name__ == "__main__":
    main()
