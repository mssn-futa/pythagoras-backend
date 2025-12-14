# **Contributing to Project Pythagoras (Backend)**

**In the name of Allah, the Most Gracious, the Most Merciful.**

Welcome to the **Backend Team** of Project Pythagoras! We are building the API engine and logic that connects Muslim students at FUTA to their spiritual and academic growth.

This document is your rulebook. Please read it carefully before writing any code.

## **The 3-Month Roadmap**

We are tackling this project in three distinct phases.

* **Phase 1: The Dawah Component (Month 1)**
  * *Focus:* Auth (JWT), Database Models (Users, Khutbahs), Mass Emailing (Celery/Redis), Admin API.  
* **Phase 2: The Academic Component (Month 2)**
  * *Focus:* Quiz Grading Logic, PDF Uploads/Storage (S3), Study Group Websockets.  
* **Phase 3: The Empowerment Hub (Month 3)**
  * *Focus:* Matching Algorithms for Mentors, Search API.

## **The "3-Day Rule"**

We value your time and your academic commitments. To prevent burnout:

**No Clickup task card should take longer than 3 days to complete.**

* If a task looks like it will take 5 days, **do not start it**.  
* Instead, ask your Backend Lead to break it down (e.g., split *"Build Auth System"* into *"Create User Model"* and *"Implement JWT Views"*).  
* If you are blocked (e.g., Database issues), raise a flag in the group chat immediately.

## **Getting Started**

### **1. Prerequisites**

Ensure you have the following installed:

* **Python** (v3.10 or higher) - [Download](https://www.python.org/)
* **Git** - [Download](https://git-scm.com/)  

### **2. Setup**

1. **Clone** the repository:

```bash
git clone https://github.com/mssn-futa/pythagoras-backend.git
cd pythagoras-backend
```

2. **Create Virtual Environment**:

```bash
python3 -m venv venv
```

Then activate it:

**Windows:**

```bash
venv\Scripts\activate
```

**Mac/Linux:**

```bash
source venv/bin/activate
```

3. **Install Dependencies**:

```bash
pip install -r requirements.txt
```

4. **Environment Variables**:
   * Copy `.env.example` to `.env`
   * Update the values as needed (email credentials, etc.)

```bash
cp .env.example .env
```

5. **Run Database Migrations**:

```bash
python manage.py migrate
```

6. **Create a Superuser** (for admin access):

```bash
python manage.py createsuperuser
```

7. **Start the Development Server**:

```bash
python manage.py runserver
```

The API will be available at `http://127.0.0.1:8000/`


## **The Workflow (Clickup -> Git -> PR)**

We follow a strict **Feature Branch Workflow**.

### **Step 1: Claim a Task**

1. Go to our **Clickup Board**.  
2. Find a card assigned to you in the **"To Do"** column (example; "Create Login Serializer").
4. Ask any clarifying questions and make sure you get all information that you need.
3. Start the task and move it to **"In Progress"**.

### **Step 2: Create a Branch**

Before creating a new branch, make sure you have the latest code from the `dev` branch:

```bash
# Fetch all remote branches
git fetch origin

# Switch to the dev branch
git checkout dev

# Pull the latest changes
git pull origin dev

# Create your new feature branch
git branch feat/khutbah-model

# Switch to your new branch
git checkout feat/khutbah-model
```

Or you can combine the last two steps:

```bash
git checkout -b feat/khutbah-model
```

**Branch Naming Convention:** `type/feature-name`

* **Types:** feat, fix, chore (maintenance), docs.
* **Example:** `feat/khutbah-model`, `fix/login-validation`, `docs/api-readme`

### **Step 3: Code & Commit**

* **Bad Message:** fixed db  
* **Good Message:** feat: added Khutbah model and applied migrations

### **Step 4: Pull Request (PR)**

1. Push your branch to remote:
   ```bash
   git push origin feat/khutbah-model
   ```

2. Go to the repository on GitHub and click **"Compare & pull request"**.

3. **Important:** Ensure the PR is configured correctly:
   - **Base:** `dev` (the branch you're merging INTO)
   - **Compare:** `feat/khutbah-model` (YOUR branch)
   
   It should look like: `base: dev` ← `compare: feat/khutbah-model`

4. **Self-Review Checklist:** 
    - Did you run `python manage.py check` with no errors? 
    - Did you remove unnecessary `print()` statements? 
    - Is it working as described on Clickup? 
    - Are you proud of it?

5. Request a review from the Backend Lead or a teammate.

## **Backend Coding Standards**

### **1. Folder Structure (Django Apps)**

We use a Domain-Driven structure. Check the `ARCHITECTURE.md` file for a better understanding.

* `apps/dawah/models.py` (Logic specific to Dawah)  
* `apps/models.py` (Do not dump everything in one file)

### **2. Django Best Practices**

* **Fat Service/Serializer, Skinny Views:** Put main logic (e.g, "Calculate Quiz Score") **Service Layer**, not in the View.  
* **Class-Based Views (CBVs):** Use APIView.
* **Naming:** Use snake_case for variables, functions, and field names. Use PascalCase for Classes.

### **3. API Standards (DRF)**

* **Response Format:** Always return JSON.  
* **Status Codes:** Use correct codes (200 OK, 201 Created, 400 Bad Request, 401 Unauthorized, 403 Forbidden).  
* **Validation:** Use **Serializers** to validate incoming data. Never trust request.data directly.

## **Need Help?**

1. Check the **Django/DRF Documentation** (It is excellent).  
2. Google the exception error.
3. Ask the backend/team lead
3. Ask in the group!

Don't forget to have fun in the process. **JazaakumuLlaahu Khayran!**