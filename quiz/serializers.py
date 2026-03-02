from rest_framework import serializers
from django.conf import settings
from .models import Course, Enrollment, Quiz, Submission, Question, Option


class CourseSerializer(serializers.ModelSerializer):
    """Serializer for Course model"""
    
    class Meta:
        model = Course
        fields = ['id', 'code', 'title', 'department', 'level', 'semester']


class EnrollmentSerializer(serializers.ModelSerializer):
    """Serializer for Enrollment model"""
    course_details = CourseSerializer(source='course', read_only=True)
    student_email = serializers.EmailField(source='student.email', read_only=True)
    
    class Meta:
        model = Enrollment
        fields = ['id', 'course', 'student', 'course_details', 'student_email']
        read_only_fields = ['id']
        
    def validate(self, data):
        """Validate unique_together constraint"""
        if Enrollment.objects.filter(
            course=data.get('course'),
            student=data.get('student')
        ).exists():
            raise serializers.ValidationError(
                "This student is already enrolled in this course."
            )
        return data


class OptionSerializer(serializers.ModelSerializer):
    """Serializer for Option model"""
    
    class Meta:
        model = Option
        fields = ['id', 'text', 'is_correct']
        read_only_fields = ['id']


class QuestionSerializer(serializers.ModelSerializer):
    """Serializer for Question model with nested options"""
    options = OptionSerializer(many=True, read_only=True, source='option_set')
    
    class Meta:
        model = Question
        fields = ['id', 'quiz', 'type', 'text', 'points', 'options']
        read_only_fields = ['id']


class QuestionCreateSerializer(serializers.ModelSerializer):
    """Serializer for creating questions with options"""
    options = OptionSerializer(many=True, required=False)
    
    class Meta:
        model = Question
        fields = ['id', 'quiz', 'type', 'text', 'points', 'options']
        read_only_fields = ['id']
    
    def create(self, validated_data):
        options_data = validated_data.pop('options', [])
        question = Question.objects.create(**validated_data)
        
        for option_data in options_data:
            Option.objects.create(question=question, **option_data)
        
        return question
    
    def update(self, instance, validated_data):
        options_data = validated_data.pop('options', None)
        
        # Update question fields
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()
        
        # Update options if provided
        if options_data is not None:
            # Delete existing options
            instance.option_set.all().delete()
            # Create new options
            for option_data in options_data:
                Option.objects.create(question=instance, **option_data)
        
        return instance


class QuizSerializer(serializers.ModelSerializer):
    """Serializer for Quiz model"""
    questions = QuestionSerializer(many=True, read_only=True, source='question_set')
    course_title = serializers.CharField(source='related_course.title', read_only=True)
    event_title = serializers.CharField(source='related_event.title', read_only=True)
    total_points = serializers.SerializerMethodField()
    question_count = serializers.SerializerMethodField()
    
    class Meta:
        model = Quiz
        fields = [
            'id', 'title', 'type', 'description', 'related_event', 
            'related_course', 'time_limit', 'is_active', 'questions',
            'course_title', 'event_title', 'total_points', 'question_count'
        ]
        read_only_fields = ['id']
    
    def get_total_points(self, obj) -> int:
        """Calculate total points for all questions in the quiz"""
        return sum(question.points for question in obj.question_set.all())
    
    def get_question_count(self, obj) -> int:
        """Get total number of questions in the quiz"""
        return obj.question_set.count()


class QuizListSerializer(serializers.ModelSerializer):
    """Lightweight serializer for listing quizzes"""
    course_title = serializers.CharField(source='related_course.title', read_only=True)
    event_title = serializers.CharField(source='related_event.title', read_only=True)
    question_count = serializers.SerializerMethodField()
    
    class Meta:
        model = Quiz
        fields = [
            'id', 'title', 'type', 'description', 'related_event',
            'related_course', 'time_limit', 'is_active', 'course_title',
            'event_title', 'question_count'
        ]
        read_only_fields = ['id']
    
    def get_question_count(self, obj) -> int:
        """Get total number of questions in the quiz"""
        return obj.question_set.count()


class SubmissionSerializer(serializers.ModelSerializer):
    """Serializer for Submission model"""
    quiz_title = serializers.CharField(source='quiz.title', read_only=True)
    student_email = serializers.EmailField(source='student.email', read_only=True)
    is_graded = serializers.SerializerMethodField()
    
    class Meta:
        model = Submission
        fields = [
            'id', 'quiz', 'student', 'answers', 'score', 
            'submitted_at', 'graded_at', 'quiz_title', 
            'student_email', 'is_graded'
        ]
        read_only_fields = ['id', 'submitted_at', 'graded_at']
    
    def get_is_graded(self, obj):
        """Check if submission has been graded"""
        return obj.graded_at is not None


class SubmissionCreateSerializer(serializers.ModelSerializer):
    """Serializer for creating submissions"""
    
    class Meta:
        model = Submission
        fields = ['id', 'quiz', 'student', 'answers', 'score']
        read_only_fields = ['id', 'score']
    
    def validate_answers(self, value):
        """Validate that answers is a valid JSON structure"""
        if not isinstance(value, dict):
            raise serializers.ValidationError("Answers must be a JSON object.")
        return value
    
    def validate(self, data):
        """Validate quiz is active"""
        quiz = data.get('quiz')
        if not quiz.is_active:
            raise serializers.ValidationError("This quiz is not currently active.")
        return data


class SubmissionDetailSerializer(serializers.ModelSerializer):
    """Detailed serializer for viewing submission with quiz details"""
    quiz = QuizSerializer(read_only=True)
    student_email = serializers.EmailField(source='student.email', read_only=True)
    is_graded = serializers.SerializerMethodField()
    
    class Meta:
        model = Submission
        fields = [
            'id', 'quiz', 'student', 'answers', 'score',
            'submitted_at', 'graded_at', 'student_email', 'is_graded'
        ]
        read_only_fields = ['id', 'submitted_at', 'graded_at']
    
    def get_is_graded(self, obj):
        """Check if submission has been graded"""
        return obj.graded_at is not None