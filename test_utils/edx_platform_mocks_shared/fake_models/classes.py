"""Mock edX classes"""
from unittest.mock import Mock


class CourseEnrollmentException(Exception):
    pass


class NonExistentCourseError(CourseEnrollmentException):
    pass


class EnrollmentClosedError(CourseEnrollmentException):
    pass


class CourseFullError(CourseEnrollmentException):
    pass


class AlreadyEnrolledError(CourseEnrollmentException):
    pass


class SvgPathImage:
    pass


class QRCode:
    def __init__(self, image_factory=None):
        self.image_factory = image_factory
        self.data_added = None
        self.made = False

    def to_string(self, encoding='unicode'):
        return '<svg>fake qr code</svg>'

    def add_data(self, data):
        self.data_added = data

    def make(self, fit=True):
        self.made = fit

    def make_image(self):
        mock_image = Mock()
        mock_image.to_string.return_value = '<svg>fake qr code</svg>'
        return mock_image
