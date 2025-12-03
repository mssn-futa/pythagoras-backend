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

1. **Fork** the backend repository to your GitHub account.  
2. **Clone** your fork:

```nginx
git clone [https://github.com/mssn-futa/pythagoras-backend.git](https://github.com/mssn-futa/pythagoras-backend.git)

cd pythagoras-backend
```

3. **Virtual Environment**:

```nginx
python3 -m venv venv
```

Then;

**Windows:**

```nginx
venv\Scripts\activate
```

**Mac/Linux:**

```nginx
source venv/bin/activate
```

4. **Install Dependencies**:

```nginx
pip install -r requirements/local.txt
```

5. **Environment Variables**:  
   * Copy .env.example to .env.  
   * Update DB credentials: DATABASE_URL=postgres://user:pass@localhost:5432/pythagoras

6. **Migrate Database**:

```nginx
python manage.py migrate
python manage.py createsuperuser
```


## **The Workflow (Clickup -> Git -> PR)**

We follow a strict **Feature Branch Workflow**.

### **Step 1: Claim a Task**

1. Go to our **Clickup Board**.  
2. Find a card assigned to you in the **"To Do"** column (example; "Create Login Serializer").
4. Ask any clarifying questions and make sure you get all information that you need.
3. Start the task and move it to **"In Progress"**.

### **Step 2: Create a Branch**

Naming Convention: `type/feature-name`

* **Types:** feat, fix, chore (maintenance), docs.
* **Example:** `feat/khutbah-model`

```nginx
git checkout -b feat/khutbah-model
```

### **Step 3: Code & Commit**

* **Bad Message:** fixed db  
* **Good Message:** feat: added Khutbah model and applied migrations

### **Step 4: Pull Request (PR)**

1. Push to your fork: `git push origin feat/khutbah-model`
2. Open a PR to the develop branch.  
3. **Self-Review:** 
    - Did you run `python manage.py test` and all tests passed? 
    - Did you remove unnecessary `print()`? 
    - Is it working as described on Clickup? 
    - Are you proud of it?

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
