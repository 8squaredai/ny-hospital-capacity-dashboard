# Business Decision App Challenge: minimal launch pad

This folder removes setup friction and nothing more. It does not choose your business problem,
data, analysis, controls, charts, layout, or conclusion. Those decisions are the project.

## Run the empty app

```bash
python3 -m venv .venv
source .venv/bin/activate        # macOS/Linux
pip install -r requirements.txt
streamlit run app.py
```

On Windows, activate with `.venv\Scripts\activate`.

## Build your own project

Use course materials, official documentation, and your AI assistant to build the app that supports
your chosen user and decision. Add every imported third-party package to `requirements.txt`.

Your final project must also contain the data file or fallback required by your source choice,
provenance, a README with setup and run instructions, verification evidence, known limitations, and
an AI-use disclosure. Follow the final-project brief for the authoritative requirements and
submission files.

Keep passwords, tokens, keys, and other secrets out of the code and repository. If the app connects
to an authenticated service, configure credentials through Streamlit secrets or another secure
mechanism. Test the deployed app in a private browser window before submission.
