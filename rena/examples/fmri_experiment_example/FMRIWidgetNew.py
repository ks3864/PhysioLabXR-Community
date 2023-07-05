# This Python file uses the following encoding: utf-8

from PyQt6.QtCore import QTimer, QThread, QMutex

from exceptions.exceptions import LSLStreamNotFoundError, ChannelMismatchError
from rena.configs.configs import AppConfigs
from rena.examples.fmri_experiment_example.mri_utils import *
# get_mri_coronal_view_dimension, get_mri_sagittal_view_dimension, \
#     get_mri_axial_view_dimension
from rena.presets.Presets import DataType
from rena.presets.presets_utils import get_stream_preset_info, set_stream_num_channels, get_stream_num_channels, \
    get_fmri_data_shape
from rena.threadings import workers
from rena.ui.SliderWithValueLabel import SliderWithValueLabel
# This Python file uses the following encoding: utf-8
import time
from collections import deque

import numpy as np
from PyQt6 import QtWidgets, uic
from PyQt6.QtCore import QTimer, Qt
from PyQt6.QtGui import QPixmap
import nibabel as nib
from nilearn import image

from rena import config, config_ui
from rena.presets.load_user_preset import create_default_group_entry
from rena.sub_process.TCPInterface import RenaTCPAddDSPWorkerRequestObject, RenaTCPInterface
from rena.ui.GroupPlotWidget import GroupPlotWidget
from rena.ui.PoppableWidget import Poppable
from rena.ui.StreamOptionsWindow import StreamOptionsWindow
from rena.ui.VizComponents import VizComponents
from rena.ui_shared import stop_stream_icon, pop_window_icon, dock_window_icon, remove_stream_icon, \
    options_icon, start_stream_icon
from rena.utils.buffers import DataBufferSingleStream
from rena.utils.dsp_utils.dsp_modules import run_data_processors
from rena.utils.ui_utils import dialog_popup


class FMRIWidget(Poppable, QtWidgets.QWidget):
    def __init__(self, parent_widget, parent_layout, stream_name, data_type, worker,
                 insert_position=None):
        """

        @param parent_widget:
        @param parent_layout:
        @param video_device_name:
        @param insert_position:
        """
        super().__init__(stream_name, parent_widget, parent_layout, self.remove_stream)

        self.ui = uic.loadUi("examples/fmri_experiment_example/FMRIWidgetNew.ui", self)
        self.setWindowTitle('fMRI Viewer')

        self.create_visualization_component()
        self.load_mri_volume()
        self.init_fmri_gl_axial_view_image_item()

        self.parent = parent_layout
        self.main_parent = parent_widget

        self.set_pop_button(self.PopWindowBtn)
        self.stream_name = stream_name
        self.data_type = data_type

        self.actualSamplingRate = 0

        self.StreamNameLabel.setText(stream_name)
        self.StartStopStreamBtn.setIcon(start_stream_icon)
        self.OptionsBtn.setIcon(options_icon)
        self.RemoveStreamBtn.setIcon(remove_stream_icon)

        self.is_stream_available = False
        self.in_error_state = False  # an error state to prevent ticking when is set to true
        # visualization data buffer
        self.current_timestamp = 0
        self.fmri_viz_volume = np.zeros((256, 256, 124))
        self._has_new_viz_data = False

        self.viz_data_buffer = None
        self.create_buffer()

        # timer
        self.timer = QTimer()
        self.timer.setInterval(config.settings.value('pull_data_interval'))
        self.timer.timeout.connect(self.ticks)

        # visualization timer
        self.v_timer = QTimer()
        self.v_timer.setInterval(int(float(config.settings.value('visualization_refresh_interval'))))
        self.v_timer.timeout.connect(self.visualize)

        # connect btn
        self.StartStopStreamBtn.clicked.connect(self.start_stop_stream_btn_clicked)
        self.OptionsBtn.clicked.connect(self.options_btn_clicked)
        self.RemoveStreamBtn.clicked.connect(self.remove_stream)

        # inefficient loading of assets TODO need to confirm creating Pixmap in ui_shared result in crash
        self.stream_unavailable_pixmap = QPixmap('../media/icons/streamwidget_stream_unavailable.png')
        self.stream_available_pixmap = QPixmap('../media/icons/streamwidget_stream_available.png')
        self.stream_active_pixmap = QPixmap('../media/icons/streamwidget_stream_viz_active.png')

        # init worker thread
        self.worker_thread = QThread(self)
        self.worker = workers.ZMQWorker(port_number=5559, subtopic='fMRI', data_type=self.data_type.value)
        self.worker.signal_data.connect(self.process_stream_data)
        self.worker.signal_stream_availability.connect(self.update_stream_availability)
        self.worker.moveToThread(self.worker_thread)
        self.worker_thread.start()

        self.tick_times = deque(maxlen=10 * int(float(config.settings.value('visualization_refresh_interval'))))
        self.setting_update_viz_mutex = QMutex()

        # start the timers

        self.timer.start()
        self.v_timer.start()

        self.__post_init__()

    def __post_init__(self):
        self.sagittal_view_slider_value = 0
        self.coronal_view_slider_value = 0
        self.axial_view_slider_value = 0

    def create_visualization_component(self):
        ######################################################
        self.volume_view_plot = gl.GLViewWidget()
        self.VolumnViewPlotWidget.layout().addWidget(self.volume_view_plot)

        ######################################################
        self.sagittal_view_plot = pg.PlotWidget()
        self.sagittal_view_plot.setTitle("Sagittal View")
        self.SagittalViewPlotWidget.layout().addWidget(self.sagittal_view_plot)
        self.sagittal_view_mri_image_item = pg.ImageItem()
        self.sagittal_view_fmri_image_item = pg.ImageItem()
        self.sagittal_view_plot.addItem(self.sagittal_view_mri_image_item)
        self.sagittal_view_plot.addItem(self.sagittal_view_fmri_image_item)

        self.sagittal_view_slider = SliderWithValueLabel()
        self.sagittal_view_slider.valueChanged.connect(self.sagittal_view_slider_on_change)
        self.SagittalViewSliderWidget.layout().addWidget(self.sagittal_view_slider)

        ######################################################
        self.coronal_view_plot = pg.PlotWidget()
        self.coronal_view_plot.setTitle("Coronal View")
        self.CoronalViewPlotWidget.layout().addWidget(self.coronal_view_plot)
        self.coronal_view_mri_image_item = pg.ImageItem()
        self.coronal_view_fmri_image_item = pg.ImageItem()
        self.coronal_view_plot.addItem(self.coronal_view_mri_image_item)
        self.coronal_view_plot.addItem(self.coronal_view_fmri_image_item)

        self.coronal_view_slider = SliderWithValueLabel()
        self.coronal_view_slider.valueChanged.connect(self.coronal_view_slider_on_change)
        self.CoronalViewSliderWidget.layout().addWidget(self.coronal_view_slider)

        ######################################################
        self.axial_view_plot = pg.PlotWidget()
        self.axial_view_plot.setTitle("Axial View")
        self.AxiaViewPlotWidget.layout().addWidget(self.axial_view_plot)
        self.axial_view_mri_image_item = pg.ImageItem()
        self.axial_view_fmri_image_item = pg.ImageItem()
        self.axial_view_plot.addItem(self.axial_view_mri_image_item)
        self.axial_view_plot.addItem(self.axial_view_fmri_image_item)

        self.axial_view_slider = SliderWithValueLabel()
        self.axial_view_slider.valueChanged.connect(self.axial_view_slider_on_change)
        self.AxiaViewSliderWidget.layout().addWidget(self.axial_view_slider)

    # def init_fmri_graphic_component(self):
    #     self.fmri_timestamp_slider = SliderWithValueLabel()
    #     self.fmri_timestamp_slider.valueChanged.connect(self.fmri_timestamp_slider_on_change)
    #     self.FMRITimestampSliderWidget.layout().addWidget(self.fmri_timestamp_slider)

    def load_mri_volume(self):
        # g = gl.GLGridItem()
        # g.scale(100, 100, 100)
        # self.volume_view_plot.addItem(g)

        _, self.mri_volume_data = load_nii_gz_file(
            'C:/Users/Haowe/OneDrive/Desktop/Columbia/RENA/RealityNavigation/rena/examples/fmri_experiment_example/structural.nii.gz')
        self.gl_volume_item = volume_to_gl_volume_item(self.mri_volume_data, non_linear_interpolation_factor=2)
        self.volume_view_plot.addItem(self.gl_volume_item)
        self.set_mri_view_slider_range()

    def set_mri_view_slider_range(self):
        # # coronal view, sagittal view, axial view
        # x_size, y_size, z_size = self.volume_data.shape
        self.coronal_view_slider.setRange(minValue=0, maxValue=get_mri_coronal_view_dimension(self.mri_volume_data) - 1)
        self.sagittal_view_slider.setRange(minValue=0,
                                           maxValue=get_mri_sagittal_view_dimension(self.mri_volume_data) - 1)
        self.axial_view_slider.setRange(minValue=0, maxValue=get_mri_axial_view_dimension(self.mri_volume_data) - 1)

        self.coronal_view_slider.setValue(0)
        self.sagittal_view_slider.setValue(0)
        self.axial_view_slider.setValue(0)

    def coronal_view_slider_on_change(self):
        self.coronal_view_slider_value = self.coronal_view_slider.value()
        self.coronal_view_mri_image_item.setImage(
            get_mri_coronal_view_slice(self.mri_volume_data, index=self.coronal_view_slider_value))

        self.set_coronal_view_fmri()

    def sagittal_view_slider_on_change(self):
        self.sagittal_view_slider_value = self.sagittal_view_slider.value()
        self.sagittal_view_mri_image_item.setImage(
            get_mri_sagittal_view_slice(self.mri_volume_data, index=self.sagittal_view_slider_value))

        self.set_sagittal_view_fmri()

    def axial_view_slider_on_change(self):
        self.axial_view_slider_value = self.axial_view_slider.value()
        self.axial_view_mri_image_item.setImage(
            get_mri_axial_view_slice(self.mri_volume_data, index=self.axial_view_slider_value))

        self.set_axial_view_fmri()

    def set_coronal_view_fmri(self):
        fmri_slice = get_mri_coronal_view_slice(self.fmri_viz_volume, self.coronal_view_slider_value)
        self.coronal_view_fmri_image_item.setImage(gray_to_heatmap(fmri_slice, threshold=0.5))

    def set_sagittal_view_fmri(self):
        fmri_slice = get_mri_sagittal_view_slice(self.fmri_viz_volume, self.sagittal_view_slider_value)
        self.sagittal_view_fmri_image_item.setImage(gray_to_heatmap(fmri_slice, threshold=0.5))

    def set_axial_view_fmri(self):
        fmri_slice = get_mri_axial_view_slice(self.fmri_viz_volume, self.axial_view_slider_value)
        self.axial_view_fmri_image_item.setImage(gray_to_heatmap(fmri_slice, threshold=0.5))
        image_data = (gray_to_heatmap(fmri_slice, threshold=0.5)*255).astype(np.uint8)
        # image_data = np.transpose(image_data, (1, 0, 2))
        self.fmri_axial_view_image_item.setData(image_data)



    def update_stream_availability(self, is_stream_available):
        '''
        this function check if the stream is available
        '''
        print('Stream {0} availability is {1}'.format(self.stream_name, is_stream_available), end='\r')
        self.is_stream_available = is_stream_available
        if self.worker.is_streaming:
            if is_stream_available:
                if not self.StartStopStreamBtn.isEnabled(): self.StartStopStreamBtn.setEnabled(True)
                self.StreamAvailablilityLabel.setPixmap(self.stream_active_pixmap)
                self.StreamAvailablilityLabel.setToolTip("Stream {0} is being plotted".format(self.stream_name))
            else:
                self.start_stop_stream_btn_clicked()  # must stop the stream before dialog popup
                self.set_stream_unavailable()
                self.main_parent.current_dialog = dialog_popup('Lost connection to {0}'.format(self.stream_name),
                                                               title='Warning', mode='modeless')
        else:
            # is the stream is not available
            if is_stream_available:
                self.set_stream_available()
            else:
                self.set_stream_unavailable()
        # self.main_parent.update_active_streams()

    def set_stream_unavailable(self):
        self.StartStopStreamBtn.setEnabled(False)
        self.StreamAvailablilityLabel.setPixmap(self.stream_unavailable_pixmap)
        self.StreamAvailablilityLabel.setToolTip("Stream {0} is not available".format(self.stream_name))

    def set_stream_available(self):
        self.StartStopStreamBtn.setEnabled(True)
        self.StreamAvailablilityLabel.setPixmap(self.stream_available_pixmap)
        self.StreamAvailablilityLabel.setToolTip("Stream {0} is available to start".format(self.stream_name))

    def set_button_icons(self):
        if not self.is_streaming():
            self.StartStopStreamBtn.setIcon(start_stream_icon)
        else:
            self.StartStopStreamBtn.setIcon(stop_stream_icon)

        if not self.is_popped:
            self.PopWindowBtn.setIcon(pop_window_icon)
        else:
            self.PopWindowBtn.setIcon(dock_window_icon)

    def options_btn_clicked(self):
        pass

    def process_stream_data(self, data_dict):
        # set visualization buffer and set has data flag

        if data_dict['frames'].shape[-1] > 0 and not self.in_error_state:

            self.viz_data_buffer.update_buffer(data_dict)
            self.actualSamplingRate = data_dict['sampling_rate']
            self.current_timestamp = data_dict['timestamps'][-1]
            self._has_new_viz_data = True
        else:
            self._has_new_viz_data = False

    def create_buffer(self):
        channel_num = get_stream_num_channels(self.stream_name)
        # buffer_size = 1 if channel_num > config.MAX_TIMESERIES_NUM_CHANNELS_PER_STREAM else config.VIZ_DATA_BUFFER_MAX_SIZE
        # self.viz_data_buffer = DataBufferSingleStream(num_channels=len(channel_names), buffer_sizes=buffer_size, append_zeros=True)

        self.viz_data_buffer = DataBufferSingleStream(num_channels=channel_num,
                                                      buffer_sizes=1, append_zeros=True)

    def visualize(self):
        self.tick_times.append(time.time())
        self.worker.signal_stream_availability_tick.emit()
        actual_sampling_rate = self.actualSamplingRate

        if not self._has_new_viz_data:
            return

        self.update_fmri_visualization()

        self.fs_label.setText(
            'Sampling rate = {:.3f}'.format(round(actual_sampling_rate, config_ui.sampling_rate_decimal_places)))
        self.ts_label.setText('Current Time Stamp = {:.3f}'.format(self.current_timestamp))
        self._has_new_viz_data = False

    def update_fmri_visualization(self):
        if self.viz_data_buffer.has_data():
            fmri_data = self.viz_data_buffer.buffer[0][:, -1]
            data_shape = get_fmri_data_shape(self.stream_name)
            self.fmri_viz_volume = np.reshape(fmri_data, data_shape)

            # self.fmri_viz_volume.normalize()
            self.set_axial_view_fmri()
            self.set_sagittal_view_fmri()
            self.set_coronal_view_fmri()

    def ticks(self):
        self.worker.signal_data_tick.emit()

    def is_streaming(self):
        return self.worker.is_streaming

    def start_stop_stream_btn_clicked(self):
        if self.worker.is_streaming:
            self.worker.stop_stream()
            if not self.worker.is_streaming:
                self.update_stream_availability(self.worker.is_stream_available)
        else:
            try:
                self.worker.start_stream()
            except LSLStreamNotFoundError as e:
                self.main_parent.current_dialog = dialog_popup(msg=str(e), title='ERROR')
                return

            except ChannelMismatchError as e:
                preset_chan_num = len(get_stream_preset_info(self.stream_name, 'num_channels'))
                message = f'The stream with name {self.stream_name} found on the network has {e.message}.\n The preset has {preset_chan_num} channels. \n Do you want to reset your preset to a default and start stream.\n You can edit your stream channels in Options if you choose Cancel'
                reply = dialog_popup(msg=message, title='Channel Mismatch', mode='modal', main_parent=self.main_parent,
                                     buttons=self.channel_mismatch_buttons)

                if reply.result():
                    self.reset_preset_by_num_channels(e.message)
                    self.worker.start_stream()

        self.set_button_icons()

    def reset_preset_by_num_channels(self, num_channels):
        set_stream_num_channels(self.stream_name, num_channels)

    def get_fps(self):
        try:
            return len(self.tick_times) / (self.tick_times[-1] - self.tick_times[0])
        except (ZeroDivisionError, IndexError) as e:
            return 0

    def is_widget_streaming(self):
        return self.worker.is_streaming

    def remove_stream(self):
        pass

    def try_close(self):
        return self.remove_stream()

    def init_fmri_gl_axial_view_image_item(self):
        # image_data = np.random.randint(0, 256, (256, 256, 4), dtype=np.uint8)
        image_data = np.zeros((256, 256, 4), dtype=np.uint8)
        self.fmri_axial_view_image_item = gl.GLImageItem(image_data) #np.zeros((256, 256, 4), dtype=np.uint8)
        self.fmri_axial_view_image_item.scale(1, -1, 1)

        # apply the xz plane transform
        self.fmri_axial_view_image_item.translate(-256 / 2, 256 / 2 , -124/2+76+0.1) #

        self.volume_view_plot.addItem(self.fmri_axial_view_image_item)
