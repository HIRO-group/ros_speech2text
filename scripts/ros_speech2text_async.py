#!/usr/bin/env python

from sys import byteorder
from array import array
from struct import pack
from std_msgs.msg import String
from ros_speech2text.msg import transcript
from google.cloud import speech
from time import time

import pyaudio
import wave
import io
import os
import rospy
import signal
import sys
import Queue
import thread

RATE = None
CHUNK_SIZE = None
SPEECH_HISTORY_DIR = None
THRESHOLD = None
FORMAT = pyaudio.paInt16
run_flag = True
DYNAMIC_THRESHOLD = None
DYNAMIC_THRESHOLD_Percentage = None
DYNAMIC_THRESHOLD_Frame = None
OPERATION_QUEUE = []

def is_silent(snd_data):
    """
    This is for static thresholding.
    Returns 'True' if below the 'silent' threshold.
    """
    rospy.loginfo(max(snd_data))
    return max(snd_data) < THRESHOLD

def is_silent_dynamic(avg_volume, snd_data):
    """
    This is for dynamic thresholding.
    Calculates if the volume of the current data frame is (100+x)% louder
    than the avg volume.
    """
    rospy.loginfo(max(snd_data))
    return max(snd_data) < avg_volume*(1+DYNAMIC_THRESHOLD_Percentage/100.0)

def normalize(snd_data):
    """
    Average the volume out
    """
    MAXIMUM = 16384
    times = float(MAXIMUM)/max(abs(i) for i in snd_data)

    r = array('h')
    for i in snd_data:
        r.append(int(i*times))
    return r

def trim(start, end, snd_data):
    """
    This function is not used in dynamic thresholding.
    Trim the blank spots at the start and end.
    """
    def _trim(snd_data):
        snd_started = False
        r = array('h')
        for i in snd_data:
            if not snd_started and abs(i)>THRESHOLD:
                snd_started = True
                r.append(i)
            elif snd_started:
                r.append(i)
        return r

    # Trim to the left
    snd_data = _trim(snd_data)
    # Trim to the right
    snd_data.reverse()
    snd_data = _trim(snd_data)
    snd_data.reverse()
    return snd_data

def add_silence(snd_data, seconds):
    """
    Add silence to the start and end of 'snd_data' of length 'seconds' (float)
    This prevents some players from skipping the first few frames.
    """
    r = array('h', [0 for i in xrange(int(seconds*RATE))])
    r.extend(snd_data)
    r.extend([0 for i in xrange(int(seconds*RATE))])
    return r

def get_next_utter(stream,min_avg_volume,pub_screen):
    """
    Main function for capturing audio.
    Parameters:
        stream: our pyaudio client
        min_avg_volume: helps thresholding in quiet environments
        pub_screen: publishes status messages to baxter screen
    """
    num_silent = 0
    snd_started = False
    stream.start_stream()
    r = array('h')
    peak_count = 0
    avg_volume = 0
    volume_queue = Queue.Queue(10)
    volume_sum = 0
    q_size = 0

    while 1:
        """
        main loop for audio capturing
        """
        if snd_started:
            pub_screen.publish("Sentence Started")
        if rospy.is_shutdown():
            return None,None,None
        # little endian, signed short
        snd_data = array('h', stream.read(CHUNK_SIZE))
        if byteorder == 'big':
            snd_data.byteswap()

        # Static thresholding
        if not DYNAMIC_THRESHOLD:
            r.extend(snd_data)
            silent = is_silent(snd_data)
            if silent and snd_started:
                num_silent += 1
            elif not silent and not snd_started:
                rospy.logwarn('collecting audio segment')
                snd_started = True
                start_time = rospy.get_rostime()
                num_silent = 0
            if snd_started and num_silent > 10:
                rospy.logwarn('audio segment completed')
                break

        """
        Dynamic thresholding
        Before audio is being considered part of a sentence, peak_count
        is used to count how many consecutive frames have been above the
        dynamic threshold volume. Once peak_count is over the specified
        frame number from ros param, we consider the sentence started and
        lock the value of avg volume to maintain the standard thresholding
        standard throughout the sentence. Whenever receiving a frame that 
        has volume that is too low, we increase num_silent. When num_silent
        exceeds ten, we consider the sentence finished.
        """
        if DYNAMIC_THRESHOLD:
            # Calculate an average volume with a queue of ten previous frames
            if volume_queue.full():
                out = volume_queue.get()
                volume_sum -= out
                q_size -= 1
            if not volume_queue.full() and peak_count==0:
                volume_queue.put(max(snd_data))
                volume_sum += max(snd_data)
                q_size += 1
            avg_volume = max(volume_sum/q_size,min_avg_volume)

            rospy.loginfo("[AVG_VOLUME] "+ str(avg_volume))
            silent = is_silent_dynamic(avg_volume, snd_data)

            if silent and snd_started:
                r.extend(snd_data)
                num_silent += 1
            elif not silent and snd_started:
                r.extend(snd_data)
            elif silent and not snd_started:
                peak_count = 0
                r = array('h')
            elif not silent and not snd_started:
                if peak_count>=DYNAMIC_THRESHOLD_Frame:
                    rospy.logwarn('collecting audio segment')
                    r.extend(snd_data)
                    start_frame = snd_data
                    start_time = rospy.get_rostime()
                    snd_started = True
                    num_silent = 0
                else:
                    peak_count += 1
                    r.extend(snd_data)
            if snd_started and num_silent > 10:
                rospy.logwarn('audio segmend completed')
                r.extend(snd_data)
                end_frame = snd_data
                break

    stream.stop_stream()
    pub_screen.publish("Recognizing")
    end_time = rospy.get_rostime()
    r = normalize(r)
    if not DYNAMIC_THRESHOLD:
        r = trim(r)
    r = add_silence(r, 0.5)
    return r,start_time,end_time

def recog(speech_client, sn, context):
    """
    Constructs a recog operation with the audio file specified by sn
    The operation is an asynchronous api call
    """
    file_name = 'sentence' + str(sn) + '.wav'
    file_path = os.path.join(SPEECH_HISTORY_DIR,file_name)
    with io.open(file_path, 'rb') as audio_file:
        content = audio_file.read()
        audio_sample = speech_client.sample(
            content,
            source_uri=None,
            encoding='LINEAR16',
            sample_rate=RATE)

    operation = speech_client.speech_api.async_recognize(sample = audio_sample, speech_context = context)
    return operation

def record_to_file(sample_width, data, sn):
    """
    Saves the audio content in data into a file with sn as a suffix of file name
    """
    data = pack('<' + ('h'*len(data)), *data)
    file_name = 'sentence' + str(sn) + '.wav'
    file_path = os.path.join(SPEECH_HISTORY_DIR,file_name)
    wf = wave.open(file_path, 'wb')
    wf.setnchannels(1)
    wf.setsampwidth(sample_width)
    wf.setframerate(RATE)
    wf.writeframes(data)
    wf.close()
    rospy.loginfo('file saved')

def expand_dir(SPEECH_HISTORY_DIR):
    """
    A function that expands directories so python can find the folder
    """
    if SPEECH_HISTORY_DIR[0]=='~':
        SPEECH_HISTORY_DIR = os.getenv("HOME") + SPEECH_HISTORY_DIR[1:]
    if not os.path.isdir(SPEECH_HISTORY_DIR):
        os.makedirs(SPEECH_HISTORY_DIR)
    return SPEECH_HISTORY_DIR

def check_operation(pub_text,pub_screen):
    """
    This function is intended to be run as a seperate thread that repeatedly
    checks if any recog operation has finished.
    The transcript returned is then published on screen of baxter and sent
    to the ros topic with the custom message type 'transcript'.
    """
    global OPERATION_QUEUE
    while not rospy.is_shutdown():
        rospy.loginfo("check operation results")
        for op in OPERATION_QUEUE[:]:
            if op[0].complete:
                for result in op[0].results:
                    msg = transcript()
                    msg.start_time = op[1]
                    msg.end_time = op[2]
                    msg.speech_duration = op[2]-op[1]
                    msg.received_time = rospy.get_rostime()
                    msg.transcript = result.transcript
                    msg.confidence = result.confidence
                    rospy.logwarn("%s,confidence:%f"%(result.transcript,result.confidence))
                    pub_text.publish(msg)
                    pub_screen.publish(result.transcript)
                OPERATION_QUEUE.remove(op)
            else:
                try:
                    op[0].poll()
                except ValueError:
                    rospy.logerr("No good results returned!")
                    OPERATION_QUEUE.remove(op)
        rospy.sleep(1)

def cleanup():
    """
    Cleans up speech history directory after session ends
    """
    speech_directory = SPEECH_HISTORY_DIR
    for file in os.listdir(speech_directory):
        file_path = os.path.join(speech_directory,file)
        try:
            os.remove(file_path)
        except Exception as e:
            rospy.logerr(e)


def main():
    global RATE
    global CHUNK_SIZE
    global THRESHOLD
    global SPEECH_HISTORY_DIR
    global FORMAT
    global DYNAMIC_THRESHOLD
    global DYNAMIC_THRESHOLD_Percentage
    global DYNAMIC_THRESHOLD_Frame
    global OPERATION_QUEUE

    # Setting up ros params
    pub_text = rospy.Publisher('/ros_speech2text/user_output', transcript, queue_size=10)
    pub_screen = rospy.Publisher('/svox_tts/speech_output', String, queue_size=10)
    rospy.init_node('speech2text_engine', anonymous=True)
    RATE = rospy.get_param('/ros_speech2text/audio_rate',16000)
    THRESHOLD = rospy.get_param('/ros_speech2text/audio_threshold',700)
    SPEECH_HISTORY_DIR = rospy.get_param('/ros_speech2text/speech_history','~/.ros/ros_speech2text/speech_history')
    SPEECH_HISTORY_DIR = expand_dir(SPEECH_HISTORY_DIR)
    input_idx = rospy.get_param('/ros_speech2text/audio_device_idx',None)
    CHUNK_SIZE = int(RATE/10)
    DYNAMIC_THRESHOLD = rospy.get_param('/ros_speech2text/enable_dynamic_threshold',False)
    DYNAMIC_THRESHOLD_Percentage = rospy.get_param('/ros_speech2text/audio_dynamic_percentage',50)
    DYNAMIC_THRESHOLD_Frame = rospy.get_param('/ros_speech2text/audio_dynamic_frame',3)
    MIN_AVG_VOLUME = rospy.get_param('/ros_speech2text/audio_min_avg',100)

    """
    Set up PyAudio client, and fetch all available devices
    Get input device ID from ros param, and attempt to use that device as audio source
    """
    p = pyaudio.PyAudio()
    device_list = [p.get_device_info_by_index(i)['name'] for i in range(p.get_device_count())]
    rospy.set_param('/ros_speech2text/available_audio_device',device_list)

    if input_idx == None:
        input_idx = p.get_default_input_device_info()['index']
    
    try:
        rospy.loginfo("Using device: " + p.get_device_info_by_index(input_idx)['name'])
        stream = p.open(format=FORMAT, channels=1, rate=RATE,input=True, start = False, input_device_index=input_idx, output=False, frames_per_buffer=CHUNK_SIZE)
    except IOError:
        rospy.logerr("Invalid device ID. Available devices listed in rosparam /ros_speech2text/available_audio_device")
        p.terminate()
        return
    sample_width = p.get_sample_size(FORMAT)

    speech_client = speech.Client()
    sn = 0

    """
    Start thread for checking operation results.
    Operations are stored in the global variable OPERATION_QUEUE
    """
    thread.start_new_thread(check_operation,(pub_text,pub_screen))


    """
    Main loop for fetching audio and making operation requests.
    """
    while not rospy.is_shutdown():
        aud_data,start_time,end_time = get_next_utter(stream,MIN_AVG_VOLUME,pub_screen)
        if aud_data == None:
            rospy.loginfo("Node terminating")
            break
        record_to_file(sample_width,aud_data, sn)
        context = rospy.get_param('/ros_speech2text/speech_context',[])
        operation = recog(speech_client, sn, context)
        OPERATION_QUEUE.append([operation,start_time,end_time])
        sn += 1

    stream.close()
    p.terminate()
    cleanup()

if __name__ == '__main__':
    main()