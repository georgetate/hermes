## hermes - summary and project purpose

The idea behind hermes is to have an interactable program that functions like a personal assistant. Aiming to assist people in industry or academia who recieve hundreds of emails daily, have complicated schedules, and are looking to ease scheduling, or reply burdens. While other programs like this already exist, hermes is designed to be unique in the way that it can easily be refactored to integrate with any email, or calendar service that has an API. This means that hermes isn't limited to one plateform but can provide user assistance across multiple inboxes, schedulers, and, as a result, facets of life.

This project is being created for the purpose of practicing George Tate's machine learning, professional programming, and real world problem solving skills.

---

### Project details
The architecture is designed in such a way as to promote modularity. Each layer is segmented from other layers. This hexagonal structure means that each service can be swapped out with an analougous service without rewiring everything around it. For example, if Microsoft calendar and Microsoft Outlook were to be added one could write and plug in a new module for them without having to worry about what the data persistance, or user interaction layers are going to see.
- Email and calendar modules (email.py and calendar.py ports) handle authorization, reads, and writes
- Data persistance modules (storage.py port) allows for the storage of information so as to minimize re-requesting data and hammering APIs
- User interaction modules (llm.py port) is the "personal assistant" which uses internal functions, stored data, and user input to interact with all the emails and calendars
- All code is written according to the specifications laid out by each port file. So that code is written to conform to internal formats and modularity can be maintained regardless of the provider (google, microsoft, OpenAI, SQL, ect)

---

### What it currently does
- Authenticates with Google services using OAuth  
- Reads Gmail inbox data and upcoming Google Calendar events  
- Normalizes email and calendar data into insternal structured Python objects  
- Persists processed data to a SQL database 
- LLM data interactions (inprogress)
- Simple CLI user interface (coming)

---

### Tech stack
- Python  
- Google Gmail API  
- Google Calendar API  
- OAuth 2.0  
- SQL (data persistence)  
- JSON / structured data processing  
