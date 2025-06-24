from django.urls import re_path
from . import views

app_name = 'payfort'

urlpatterns = [
    re_path(r'^return/$', views.PayFortReturnView.as_view(), name='return'),
    re_path(r'^feedback/$', views.PayfortFeedbackView.as_view(), name='feedback'),
    re_path(r'^status/$', views.PayFortStatusView.as_view(), name='status'),
]
