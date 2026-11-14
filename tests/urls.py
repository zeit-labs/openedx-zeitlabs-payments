"""URLs configuration for testing purposes."""
from django.contrib import admin
from django.urls import include, path
from django.views import View

urlpatterns = [
    # Admin URLs
    path('admin/', admin.site.urls),

    # LMS URL names the plugin templates reverse rather than hard-coding a path. Only reversed, never requested,
    # so a bare View stands in for each: there is no response body to write, and none for coverage to miss.
    path('dashboard/', View.as_view(), name='dashboard'),
    path('courses/', View.as_view(), name='courses'),
    path('courses/<str:course_id>/about', View.as_view(), name='about_course'),

    # Plugin urls
    path('', include('zeitlabs_payments.urls')),
]
