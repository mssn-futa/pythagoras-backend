# Backend Architecture & Structure: Project Pythagoras

**Version:** 1.0 | **Phase:** 1 (Dawah Component)

**Stack:** Django + Django REST Framework (DRF) + SQLite

## 1. High-Level Architecture

This Django backend will serve strictly as an API provider. We do not use Django Templates for the frontend.

### The Request/Response Cycle

When the React frontend requests data (for example, "Get all Khutbahs"), the flow moves through these layers:


```nginx

     REQUEST (JSON)           ROUTING LAYER            LOGIC LAYER             DATA LAYER  
+-----------------------+   +----------------+   +--------------------+   +------------------+  
|                       |   |                |   |                    |   |                  |  
|  React Frontend       |-->|  urls.py       |-->|  ViewSet / APIView |-->|  Serializer      |  
|  (Axios Client)       |   |  (Dispatcher)  |   |  (Business Logic)  |   |  (Validation)    |  
|                       |   |                |   |                    |   |                  |  
+-----------------------+   +----------------+   +--------------------+   +------------------+  
          ^                                                                        |  
          |                                                                        v  
          |                                                                 +------------------+  
          |                                                                 |                  |  
          +--------------------(JSON Response)------------------------------|  Models (ORM)    |  
                                                                            |  (PostgreSQL)    |  
                                                                            |                  |  
                                                                            +------------------+

```

## 2. Django App Modules

We split the backend into specific "Apps" to prevent a mess. Each app handles one specific domain of the project.

```nginx
[ Project Pythagoras ]  
       |  
       +--- [ accounts ]     <-- Identity. (Users, Auth, Profiles, JWT)  
       |  
       +--- [ dawah ]        <-- Content. (Khutbahs, Audios, Events, Announcements)  
       |  
       +--- [ quiz ]         <-- Academics Phase 1. (Tests/Exams, Questions, Results)  
       |  
       +--- [ pythagoras ]         <-- Utilities. (Base Models, Permissions, Global Config)
```


### App Responsibilities

| App | Responsibility | Key Models |
| :---- | :---- | :---- |
| **accounts** | Auth & User Management | User, StudentProfile (Level, Dept) |
| **dawah** | Content Management | Khutbah, Event, Announcement |
| **quiz** | Assessments | Exam, Question, Choice, Submission |
| **pythagoras** | Shared Utils | TimeStampedModel, StandardResultsSetPagination |


## **3. The File Structure**

We use a modular Django layout. Configuration is separated from the apps.

### **Directory Tree**

```nginx
pythagoras-backend/  
├── manage.py                # Entry point  
├── config/                  # Project Settings  
│   ├── settings/  
│   │   ├── base.py          # Shared settings (Apps, Middleware)  
│   │   ├── local.py         # Dev settings (Debug=True)  
│   │   └── production.py    # Prod settings 
│   ├── urls.py              # Main URL Router  
│   └── wsgi.py              # Server Gateway  
├── apps/                    # ALL CUSTOM APPS LIVE HERE  
│   ├── accounts/  
│   │   ├── models.py        # User, StudentProfile  
│   │   ├── serializers.py   # UserSerializer, RegisterSerializer  
│   │   ├── views.py         # LoginView, ProfileView  
│   │   └── urls.py  
│   ├── dawah/  
│   │   ├── models.py        # Khutbah, Event  
│   │   ├── serializers.py  
│   │   └── views.py  
│   ├── quiz/  
│   │   ├── models.py        # Exam, Question  
│   │   ├── services.py      # Grading Logic (calculate_score)  
│   │   └── views.py  
│   └── core/  
│       ├── settings.py
│       └── wsgi.py   
├── media/                   # Uploaded files (Audio/Images) - Dev only  
└── requirements.txt            # Dependencies
```


## 4. Detailed Data Flow & Model Relations

### A. The Dawah Flow (Uploading & Fetching Khutbahs)

1. **Admin Upload:** Admin POSTs audio file -> dawah.views.KhutbahView -> KhutbahSerializer -> Saves to Media service -> Returns ID.  
2. **Student Fetch:** Student GETs list -> dawah.views.KhutbahView -> KhutbahSerializer -> Returns JSON List.

### **B. The Quiz Flow (Grading Logic)**

This is a bit more involving, so we separate logic into a services.py file inside the quiz app.

```nginx
[ Student Submits Quiz ]  
       |  
       v  
[ QuizSubmissionView ]  
       |  
       v  
[ QuizService.grade_submission() ]  <-- PURE PYTHON LOGIC  
       | 1. Fetch correct answers from DB  
       | 2. Compare with student answers  
       | 3. Calculate Score  
       | 4. Create 'Result' record  
       |  
       v  
[ Return Score to Frontend ]
```

## **5. Coding Guidelines**

### **A. General Rules**

* **Fat Services/Serializers, Skinny Views:** Put logic in Serialozers or Services, not in Views.  
* **CBVs Only:** Use Class-Based Views (APIViews). 
* **Snake_Case:** Python uses snake_case for variables and functions so let's keep to that convention.


### **B. Example Code Pattern**

This is an example of the coing standard we'd like to adapt in this project. Endeavour to make your implementations
follow a pattern simila to this so we can have a uniform codebase (and less fight during code reviews).

**1. The Model (apps/dawah/models.py)**

```python
from django.db import models  
from apps.core.models import TimeStampedModel

class Khutbah(TimeStampedModel):  
    title = models.CharField(max_length=255)  
    khatib = models.CharField(max_length=255)  
    audio_file = models.FileField(upload_to='khutbahs/')  
    date = models.DateField()

    def __str__(self):  
        return f"{self.title} by {self.khatib}"
```

**2. The Serializer (apps/dawah/serializers.py)**

```python
from rest_framework import serializers  
from .models import Khutbah

class KhutbahSerializer(serializers.ModelSerializer):  
    class Meta:  
        model = Khutbah  
        fields = ['id', 'title', 'khatib', 'audio_file', 'date', 'created_at']
```


**3. The View (apps/dawah/views.py)**

```python
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import IsAuthenticatedOrReadOnly  
from .models import Khutbah  
from .serializers import KhutbahSerializer

class KhutbahView(APIView):  
    """  
    Public can List/Retrieve. 
    Only Admin can Create/Update/Delete.  
    """  
    permission_classes = [IsAuthenticatedOrReadOnly]
    
    def get(self, request):
        query = Khutbah.objects.all().order_by('-date')  
        serializer_class = KhutbahSerializer(query, many=True)
        return Response({
            "success": True, 
            "data": serializer.data, 
            "message": "Kuthbahs retrieved successully"
        }, status=status.HTTP_200_OK)
```