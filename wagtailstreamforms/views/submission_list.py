import csv
import datetime

from collections import OrderedDict

from django.core.exceptions import PermissionDenied, FieldDoesNotExist
from django.http import Http404, HttpResponse
from django.utils.dateformat import Formatter
from django.utils.encoding import force_str
from django.utils.html import strip_tags
from django.utils.translation import ugettext as _
from django.views.generic import ListView
from django.utils.formats import get_format
from django.views.generic.detail import SingleObjectMixin
from wagtail.contrib.modeladmin.helpers import PermissionHelper

from wagtailstreamforms import hooks
from wagtailstreamforms.forms import SelectDateForm
from wagtailstreamforms.models import Form
from xlsxwriter.workbook import Workbook


def list_to_str(value):
    return force_str(", ".join(value))


class ExcelDateFormatter(Formatter):
    data = None

    _formats = {
        "d": "DD",
        "j": "D",
        "D": "NN",
        "l": "NNNN",
        "S": "",
        "w": "",
        "z": "",
        "W": "",
        "m": "MM",
        "n": "M",
        "M": "MMM",
        "b": "MMM",
        "F": "MMMM",
        "E": "MMM",
        "N": "MMM.",
        "y": "YY",
        "Y": "YYYY",
        "L": "",
        "o": "",
        "g": "H",
        "G": "H",
        "h": "HH",
        "H": "HH",
        "i": "MM",
        "s": "SS",
        "u": "",
        "a": "AM/PM",
        "A": "AM/PM",
        "P": "HH:MM AM/PM",
        "e": "",
        "I": "",
        "O": "",
        "T": "",
        "Z": "",
        "c": "YYYY-MM-DD HH:MM:SS",
        "r": "NN, MMM D YY HH:MM:SS",
        "U": "[HH]:MM:SS",
    }

    def get(self):
        format = get_format("SHORT_DATETIME_FORMAT")
        return self.format(format)

    def __getattr__(self, name):
        if name in self._formats:
            return lambda: self._formats[name]
        raise AttributeError(
            f"'{type(self).__name__}' object has no attribute '{name}'"
        )


class SubmissionListView(SingleObjectMixin, ListView):
    paginate_by = 25
    page_kwarg = "p"
    template_name = "streamforms/index_submissions.html"
    filter_form = None
    model = Form

    FORMAT_XLSX = "xlsx"
    FORMAT_CSV = "csv"
    FORMATS = (FORMAT_XLSX, FORMAT_CSV)

    # A list of fields or callables (without arguments) to export from each item in the queryset (dotted paths allowed)
    list_export = []
    # A dictionary of custom preprocessing functions by field and format (expected value would be of the form {field_name: {format: function}})
    # If a valid field preprocessing function is found, any applicable value preprocessing functions will not be used
    custom_field_preprocess = {}
    # A dictionary of preprocessing functions by value class and format
    custom_value_preprocess = {
        (datetime.date, datetime.time): {FORMAT_XLSX: None},
        list: {FORMAT_CSV: list_to_str, FORMAT_XLSX: list_to_str},
    }
    # A dictionary of column heading overrides in the format {field: heading}
    export_headings = {}

    @property
    def permission_helper(self):
        return PermissionHelper(model=self.model)

    def dispatch(self, request, *args, **kwargs):
        self.object = self.get_object()
        if not self.permission_helper.user_can_list(self.request.user):
            raise PermissionDenied
        return super().dispatch(request, *args, **kwargs)

    def get_object(self, queryset=None):
        pk = self.kwargs.get(self.pk_url_kwarg)
        try:
            qs = self.model.objects.all()
            for fn in hooks.get_hooks("construct_form_queryset"):
                qs = fn(qs, self.request)
            return qs.get(pk=pk)
        except self.model.DoesNotExist:
            raise Http404(_("No Form found matching the query"))

    def get(self, request, *args, **kwargs):
        self.filter_form = SelectDateForm(request.GET)

        if request.GET.get("action") == "CSV":
            return self.csv()

        if request.GET.get("action") == "EXCEL":
            return self.write_xlsx_response(self.get_queryset())

        return super().get(request, *args, **kwargs)

    def csv(self):
        queryset = self.get_queryset()
        data_fields = self.object.get_data_fields()
        data_headings = [force_str(strip_tags(label)) for name, label in data_fields]

        response = HttpResponse(content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = "attachment;filename=export.csv"

        writer = csv.writer(response)
        writer.writerow(data_headings)
        for s in queryset:
            data_row = []
            form_data = s.get_data()
            for name, label in data_fields:
                data_row.append(force_str(form_data.get(name)))
            writer.writerow(data_row)

        return response

    def get_preprocess_function(self, field, value, export_format):
        """ Returns the preprocessing function for a given field name, field value, and export format"""

        # Try to find a field specific function and return it
        format_dict = self.custom_field_preprocess.get(field, {})
        if export_format in format_dict:
            return format_dict[export_format]

        # Otherwise check for a value class specific function
        for value_classes, format_dict in self.custom_value_preprocess.items():
            if isinstance(value, value_classes) and export_format in format_dict:
                return format_dict[export_format]

        # Finally resort to force_str to prevent encoding errors
        return force_str

    def write_xlsx_row(self, worksheet, row_dict, row_number):
        for col_number, (field, value) in enumerate(row_dict.items()):
            preprocess_function = self.get_preprocess_function(
                field, value, self.FORMAT_XLSX
            )
            processed_value = (
                preprocess_function(value) if preprocess_function else value
            )
            worksheet.write(row_number, col_number, processed_value)

    def write_xlsx(self, queryset, output):
        """ Write an xlsx workbook from a queryset"""
        workbook = Workbook(
            output,
            {
                "in_memory": True,
                "constant_memory": True,
                "remove_timezone": True,
                "default_date_format": ExcelDateFormatter().get(),
            },
        )
        worksheet = workbook.add_worksheet()

        data_fields = self.object.get_data_fields()
        data_headings = [force_str(strip_tags(label)) for name, label in data_fields]

        for col_number, field in enumerate(data_headings):
            worksheet.write(0, col_number, field)

        for row_number, item in enumerate(queryset):
            data_dict = OrderedDict()
            form_data = item.get_data()
            for name, label in data_fields:
                data_dict[label] = force_str(form_data.get(name))
            self.write_xlsx_row(worksheet, data_dict, row_number + 1)

        workbook.close()

    def write_xlsx_response(self, queryset):
        response = HttpResponse(
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
        response["Content-Disposition"] = 'attachment; filename="{}.xlsx"'.format(
            'export'
        )
        self.write_xlsx(queryset, response)

        return response

    def get_queryset(self):
        submission_class = self.object.get_submission_class()
        self.queryset = submission_class._default_manager.filter(form=self.object)

        # filter the queryset by the required dates
        if self.filter_form.is_valid():
            date_from = self.filter_form.cleaned_data.get("date_from")
            date_to = self.filter_form.cleaned_data.get("date_to")
            if date_from:
                self.queryset = self.queryset.filter(submit_time__gte=date_from)
            if date_to:
                date_to += datetime.timedelta(days=1)
                self.queryset = self.queryset.filter(submit_time__lte=date_to)

        return self.queryset.prefetch_related("files")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        data_fields = self.object.get_data_fields()
        data_headings = [label for name, label in data_fields]

        # populate data rows from paginator
        data_rows = []
        for s in context["page_obj"]:
            form_data = s.get_data()
            form_files = s.files.all()
            data_row = [form_data.get(name) for name, label in data_fields]
            data_rows.append(
                {"model_id": s.id, "fields": data_row, "files": form_files}
            )

        context.update(
            {
                "filter_form": self.filter_form,
                "data_rows": data_rows,
                "data_headings": data_headings,
                "has_delete_permission": self.permission_helper.user_can_delete_obj(
                    self.request.user, self.object
                ),
            }
        )

        return context
