<p align="center">
  <img src="docs/banner.png" alt="Taqdeer: AI-assisted exam grading" width="720">
</p>

<p align="center"><b>AI-assisted grading for paper and digital exams, with topic-level learning gap analytics</b></p>

<p align="center">
  <a href="https://taqdeerdemo.netlify.app"><b>Live demo</b></a> ·
  Senior Project, B.Sc. Information Systems, King Abdulaziz University (2026) ·
  1st place, Senior Projects Exhibition, Faculty of Computing and IT, Rabigh
</p>

---

## The problem

Grading written exams by hand takes days, scores vary between graders, and it is hard to see which topics a whole class struggled with. In a survey of 6 instructors, all 6 said manual grading is a heavy time burden, and 5 of 6 said they find it hard to spot topic-level learning gaps.

## What Taqdeer does

Taqdeer takes an exam from uploaded answer files to approved grades and class insights, while the instructor stays in control of every final mark. It works with digital answer files and with scanned handwritten papers.

1. **Exam setup.** The instructor creates the exam: course, section, number of students and total marks.
2. **Answer key extraction.** The answer key, digital or scanned, is turned into structured questions, model answers and marks.
3. **Student answer extraction.** Student answer files are read with AI. For scanned paper, this includes handwritten text, selected options and hand-drawn diagrams.
4. **Consensus grading.** Claude and ChatGPT grade each answer independently. When they disagree, a consensus step resolves the score and records the reasoning.
5. **Instructor review.** The instructor sees every score with the AI's reasoning, edits any mark (tracked as an override) and approves the results.
6. **Learning gap analytics.** After approval, the Qwen model analyses the class by question and topic, rates each gap by severity, explains the misconception and suggests a teaching action.
7. **Export and archive.** Results export to CSV, Excel or PDF, and past exams can be archived and regraded.

Supported question types: short answer, comparison, mathematical, multiple choice, true/false, and network (AOA) diagrams.

## Try the demo

**[taqdeerdemo.netlify.app](https://taqdeerdemo.netlify.app)** runs the real interface on sample data, with no sign-up. A short guide walks you through each step. All names, exams and answers in the demo are fictional.

## Tech stack

| Layer | Technology |
| --- | --- |
| Frontend | HTML, CSS, JavaScript |
| Backend | Python, FastAPI (REST API, JWT authentication, role-based access) |
| Database | MySQL with SQLAlchemy |
| AI models | Claude (extraction and grading), ChatGPT (grading), Qwen (learning gap analysis) |
| PDF processing | PyMuPDF, Pillow |
| Testing and design | Thunder Client (API testing), Draw.io (UML and ER diagrams) |

## Testing

- Validated on anonymized real final exams from an IS Project Management course, covering 5 question types.
- Unit, integration, API and system testing.
- Usability testing with 6 participants (instructors, evaluators and academic users); all 12 core tasks were completed successfully.

## Project structure

```
backend/
  main.py            FastAPI app and startup
  routers/           API endpoints (exams, extraction, grading, results, gap analysis, exports, auth)
  services/          Extraction, grading engine, prompts, model clients, gap analysis
  models.py          Database tables
  schemas.py         Request and response models
frontend/
  index.html         Landing page
  pages/             Instructor and admin pages
  js/ css/ assets/
grading_notebook/    Early grading prototype
scripts/             Maintenance scripts
```

## Running locally

Requirements: Python 3.9+, MySQL, and API keys for the AI models you want to use.

```bash
cd backend
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # then fill in your own values
uvicorn main:app --reload --port 8000
```

Create an empty MySQL database named `taqdeer`; the tables are created on first start. Then open `frontend/index.html` with a local static server (for example, the VS Code Live Server extension).

## Privacy

Participants gave their consent through a privacy and participation consent form before taking part. Student exam papers used during development were anonymized before being sent to any AI model. No exam files or student data are included in this repository.

## Team

- Elaf Alyoubi
- Alyah Alshareef
- Norma Atawi

Supervised by Dr. Rana Alshaikh, Department of Information Systems, Faculty of Computing and Information Technology, King Abdulaziz University, Rabigh.
