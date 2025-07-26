
---

# PKG NAME📘: `devDocs` – AI-powered automated project documentation writer 

`devDocs` is a **command-line tool** that automatically creates high-quality `README.md` files by analyzing your project’s **folder structure**, **source code**, and any existing documentation. It uses the **Google Gemini API** to generate clear, structured, and professional Markdown documentation.

Perfect for:

* Open-source contributors 💡
* Developers maintaining internal tools 🛠️
* Hackathon projects needing clean docs fast 🚀

---

## 📂 Example Project Structure

Here’s how your project might look before and after using `devDocs`:

```bash
your-project/
├── src/
│   ├── main.py
│   └── utils.py
├── tests/
├── requirements.txt
├── LICENSE
└── README.md   <-- Generated/Overwritten by devDocs
```

---

## ⚙️ How It Works (Behind the Scenes)

Here's what happens when you run `devDocs`:

1. **Scans your project** – Analyzes directory structure, code files, and existing README files.
2. **Parses content** – Gathers code and documentation from each relevant file/folder.
3. **Generates documentation** – Sends context to Google Gemini API to craft a structured `README.md`.
4. **Saves output** – Writes the generated Markdown into your project (or into a custom output folder).

🔁 Optional features:

* Preserves your original README unless you use `--overwrite`.
* Includes/excludes specific files or folders with filters.

---

## 📦 Installation

Install from PyPI:

```bash
pip install devDocs
```

---

## 🔑 Requirements

* **Python 3.8+**
* **Google Gemini API Key**
  Get one from [Google AI Studio](https://aistudio.google.com/).

---

## 🚀 Usage

Inside the root folder of your project, run:

```bash
devDocs [OPTIONS]
```

The CLI will prompt for your **Google Gemini API key**. Paste it once when asked.

### CLI Options

| Option          | Description                                                   |
| --------------- | ------------------------------------------------------------- |
| `--path`        | Root path to scan (default: current directory)                |
| `--name`        | Project name to display in the README                         |
| `--description` | Short description for the project                             |
| `--authors`     | Comma-separated list of authors                               |
| `--keywords`    | Comma-separated list of keywords (e.g., cli, docs, auto)      |
| `--overwrite`   | Overwrite existing `README.md` files (default: False)         |
| `--output`      | Output folder to save generated docs (default: `docs/`)       |
| `--exclude`     | Comma-separated folders/files/extensions to **exclude**       |
| `--include`     | Comma-separated folders/files/extensions to **force include** |

---

### ✅ Example Command

```bash
devDocs --path . \
        --name "Cool Dev Tool" \
        --description "Generate AI-based READMEs effortlessly" \
        --authors "Gantavya Bansal" \
        --keywords "cli, docs, automation, openai" \
        --output docs \
        --overwrite
```

This will:

* Walk through all folders from current directory
* Create a `docs/README.md` and other structured markdowns
* Overwrite existing README if one exists

---

## 🧠 Features

* ✅ Generates structured, professional `README.md` files automatically
* ✅ Preserves original docs unless `--overwrite` is set
* ✅ Supports **include/exclude** filtering for granular control
* ✅ Smart project tree visualization included in docs
* ✅ Outputs all documentation to a single folder (`--output`)
* ✅ Powered by Google Gemini AI (clean & readable Markdown)

---

## 🏗️ Example Output (Generated)

Here’s a sample snippet of what the generated README might look like:

```
# Cool Dev Tool

This is a CLI tool for generating clean README.md files using Google Gemini.

## Folder Structure
your-project/
├── src/
│   ├── main.py
│   └── utils.py
├── README.md
...

## Usage
...
```

---

## 🧱 Technologies Used

* `Python 3.8+`
* [`google-genai`](https://pypi.org/project/google-generativeai/)
* `argparse`, `os`, `logging`, `time` – for CLI and system interaction

---

## 🧰 Developer Notes

If you're contributing or extending this project:

### Core Files

| File               | Purpose                                         |
| ------------------ | ----------------------------------------------- |
| `cli.py`           | CLI interface + core logic                      |
| `README.md`        | The README template output (can be regenerated) |
| `LookFolder()`     | Recursive folder/file scanner                   |
| `GenerateReadMe()` | Sends data to Gemini and processes results      |
| `print_tree()`     | Generates folder structure view in tree format  |

### Data Flow

1. CLI parses args →
2. Filters folders/files →
3. Reads source + existing docs →
4. Calls `GenerateReadMe()` →
5. Writes Markdown to output

### API Instruction Logic (Simplified)

```python
system_instruction = '''
You are Gantavya Bansal, a senior engineer and tech writer.
Generate clean, professional Markdown documentation using code + structure context.
Include:
- Title
- Folder Tree
- Description
- Usage
- Tech Stack
- Known Issues
- Licensing
'''
```

---

## ⚠️ Known Limitations

* 📡 Needs an internet connection for Gemini API
* 🔁 Limited retry logic for failed API calls
* ⚙️ Include/exclude filters don't yet support regex
* 📄 Only supports `.md` output format

---

## 📜 License

**MIT License** – You’re free to use, modify, and share.
Attribution is appreciated!

---

## 💬 Contributing

Feel free to open issues, suggest improvements, or contribute directly.
Pull requests are always welcome!

---
