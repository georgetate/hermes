## hermes – Python Workflow Automation (Gmail & Calendar)

hermes is a Python-based workflow automation tool that integrates with Gmail and Google Calendar APIs to extract, normalize, and process inbox and scheduling data.

The project focuses on building clean, modular Python code for API integrations, authentication, data handling, and persistence that can be reused or extended for custom automation workflows.

---

### What it does
- Authenticates with Google services using OAuth  
- Reads Gmail inbox data and upcoming Google Calendar events  
- Normalizes email and calendar data into structured Python objects  
- Persists processed data to a SQL database  
- Generates basic summaries and suggestions from retrieved data  

---

### Why this project exists
This repository demonstrates:
- Python automation interacting with real-world APIs  
- OAuth-based authentication flows  
- API data extraction and transformation  
- Structured data storage using SQL  
- Clean, extensible project architecture suitable for automation scripts  

The architecture is designed so additional processing steps or integrations can be added with minimal changes.

---

### Current status
- Functional core features implemented  
- Actively iterating on data processing and output logic  
- Read-only access to Google services (no destructive actions)

---

### Tech stack
- Python  
- Google Gmail API  
- Google Calendar API  
- OAuth 2.0  
- SQL (data persistence)  
- JSON / structured data processing  
