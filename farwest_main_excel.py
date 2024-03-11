# -*- coding: utf-8 -*-
"""
Created on Thu Feb  1 12:43:09 2024
@author: aadi
"""


import cv2
import threading
import time
import os
import sys
import keyboard
import pandas as pd
from datetime import datetime
from collections import defaultdict  # For creating a dictionary with default values
import supervision as sv  # For counting unique objects in detection results
import numpy as np
import json

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google.oauth2 import service_account


SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
SERVICE_ACCOUNT_FILE = 'keys.json' #replace with the key file name that you downloaded

creds = None
creds = service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE, scopes=SCOPES)

# The ID and range of a sample spreadsheet.
SPREADSHEET_ID = "11o-ZoNmn4-FdCHd0gJuZMrUC-3mYFgo2EheHKIJfhvE" #Give the ID of the Google spreadsheet



# Add YOLOv5 folder to the sys.path
yolov5_path = "../yolov5_farwest"   # Adjust as needed
sys.path.append(yolov5_path)

# Import the run function
from detect import run, load_model

### YOLO model
weights = os.path.join(yolov5_path, 'check.pt')
print('check.pt')

iou_thres = 0.05
conf_thres = 0.65
augment = True
debug_save = False
device = "CPU"
response_as_bbox = True
if_cloud = False

# Load the model
model, stride, names, pt = load_model(weights=weights, device=device)

# Initialize variables for frame processing and detection counts
if response_as_bbox:
    ct = defaultdict(int)  # Dictionary to count detected objects by category
    augment = False
else:
    ct = {0: 0, 1: 0, 2: 0, 3: 0}
    
current_frame = None  # Variable to store the current frame for processing
frame_lock = threading.Lock()  # Lock for thread-safe operations on the current frame
stop_threads = False  # Flag to control the stopping of threads
frame_counter = 0  # Counter for processed frames
total_duration = 0
sv_cons_frames = 4 # Number of consecutive frames to consider an object as detected
# KMP_DUPLICATE_LIB_OK=TRUE
os.environ['KMP_DUPLICATE_LIB_OK']='True'
os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp"


def unflatten(input_string):
    values = input_string.split(',')
    first_value = values[0]
    grouped_values = [values[i:i+3] for i in range(1, len(values), 3)]
    return first_value, grouped_values


def count_first_items(matrix, counts):
    for inner_list in matrix:
        if inner_list:
            first_item = inner_list[0]
            if int(first_item) in counts:
                counts[int(first_item)] += 1
    return counts


def append_data(my_dict):
    current_timestamp = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
    # Initialize value list with zeros for keys 0, 1, 2, 3
    value = [0, 0, 0, 0]
    
    # Update the value list based on keys present in my_dict
    for key in range(4):  # Assuming keys are 0, 1, 2, 3
        if key in my_dict:
            value[key] = my_dict[key]
    
    # Insert the current_timestamp at the beginning of the value list
    value.insert(0, current_timestamp)
    value = [value]  # Ensure it is a 2D list to match the API requirement

    try:
        start = time.time()
        service = build("sheets", "v4", credentials=creds)
        sheet = service.spreadsheets()
        
        result = (
            sheet.values()
            .append(spreadsheetId=SPREADSHEET_ID, range="camera!A1", 
                    valueInputOption="USER_ENTERED",
                    insertDataOption="INSERT_ROWS",
                    body={"values": value})
            .execute()
        )
        
        end = time.time()
        # print("Time taken to append data to Google Sheet: ", end - start)
    except HttpError as err:
        print(err)


def read_frames(cap):
    global current_frame, stop_threads
    while not stop_threads:
        if keyboard.is_pressed('esc'):
            stop_threads = True
            break
        ret, frame = cap.read()
        if not ret:
            stop_threads = True
            break
        with frame_lock:
            current_frame = frame


def detect_and_display():
    """
    Perform object detection on captured frames and display the results.
    """
    global current_frame, stop_threads, frame_counter, ct, check, total_duration
    tracker = sv.ByteTrack()            # Bytetrack takes a number of optional arguments, TODO tuning
    seen_ids = []
    consecutive_frames = defaultdict(int)
    
    while not stop_threads:
        start_time = time.time()
        if keyboard.is_pressed('esc'):  # Listen for ESC key to stop
            stop_threads = True
            break

        local_frame = None
        with frame_lock:
            if current_frame is not None:
                local_frame = current_frame.copy()
                current_frame = None

        if local_frame is not None:
            
            # Run detection on the temporary image file
            output = run(weights=weights, source=local_frame, iou_thres=iou_thres,
                         conf_thres=conf_thres, augment=augment, model=model, stride=stride,
                         names=names, pt=pt, debug_save=debug_save, response_as_bbox=response_as_bbox)
           
            # print(output)
            
            if not response_as_bbox:
                # Every 10 frames
                # if frame_counter % 14 == 0:
                #     for detection in output:
                #         ct[detection[2]] += 1
                if frame_counter % 14 == 0:
                    a, unflattened_lst = unflatten(output)
                    check = count_first_items(unflattened_lst, ct)
            else:
                # Turn the first element in each tuple in output list into a np array
                xyxy = np.array([np.array([int(d[0][0]), int(d[0][1]), int(d[0][2]), int(d[0][3])]) for d in output])
                # Second element is the confidence score
                confs = np.array([d[1] for d in output])
                # Third element is the category id
                labels = np.array([d[2] for d in output])

                # Feed this to supervision
                detections = sv.Detections(xyxy, confidence=confs, class_id=labels)
                detections = tracker.update_with_detections(detections)

                try:
                    for class_id, tracker_id in zip(detections.class_id, detections.tracker_id):
                        if tracker_id not in seen_ids:
                            consecutive_frames[tracker_id] += 1
                            if consecutive_frames[tracker_id] >= sv_cons_frames:
                                seen_ids.append(tracker_id)
                                seen_ids = seen_ids[-30:]
                                ct[class_id] += 1
                        # Delete IDs that are not tracked consistently
                        if tracker_id not in detections.tracker_id:
                            consecutive_frames.pop(tracker_id, None)
                except TypeError as e:
                    # Sometimes the detections are empty, and zip doesn't like that
                    print(e)
                    pass

            end_time = time.time()

            # Calculate and accumulate the duration
            duration = end_time - start_time
            total_duration += duration
            frame_counter += 1            

            # print(f"Received response for frame {frame_counter}: ", response_msg)
            # print(f"Duration for this frame: {duration:.3f} seconds")
            # print ("==================================")


def send_image(video_path):
   
    global stop_threads, ct
    cap = cv2.VideoCapture(video_path,cv2.CAP_FFMPEG)
    if not cap.isOpened():
        print("Error: Could not open stream.")
        return

    # Starting threads for reading frames and detecting objects
    thread_read = threading.Thread(target=read_frames, args=(cap,))
    thread_detect = threading.Thread(target=detect_and_display)
    thread_read.start()
    thread_detect.start()

    last_save_time = time.time()

    try:
        while not stop_threads:
            if keyboard.is_pressed('esc'):  # Allow manual interruption
                stop_threads = True

            current_time = time.time()
            if current_time - last_save_time >= 3600:  # Check if an hour has passed
                if response_as_bbox:
                    print(dict(ct))
                    append_data(dict(ct))
                    print("Hourly counts appended to Google Sheets.")
                    if if_cloud == True:
                        json_string = json.dumps(dict(ct))
                    # Reset the counts
                    ct.clear()
                    
                else:
                    print(check)
                    append_data(check)
                    print("Hourly counts appended to Google Sheets.")
                    if if_cloud == True:
                        json_string = json.dumps(check)
                    # Reset the counts
                    ct = {0: 0, 1: 0, 2: 0, 3: 0}
                
                
                last_save_time = current_time

            time.sleep(1)  # Sleep to reduce CPU usage

    except KeyboardInterrupt:
        stop_threads = True

    finally:
        
        if response_as_bbox:
            print(dict(ct))
            append_data(dict(ct))
            if if_cloud == True:
                json_string = json.dumps(dict(ct))
            # Reset the counts
            ct.clear()
            
        else:
            print(check)
            append_data(check)
            if if_cloud == True:
                json_string = json.dumps(check)
            # Reset the counts
            ct = {0: 0, 1: 0, 2: 0, 3: 0}
        
        last_save_time = current_time
        
        thread_read.join()
        thread_detect.join()
        cap.release()


def main():
    # Paths
    video_path = "rtsp://:8554/"
    # Append options to force the use of TCP
    video_path_tcp = video_path + "?transport=tcp"
    # video_path = "../recycle_small_test_slow.mp4"
    send_image(video_path_tcp)

if __name__ == "__main__":
    main()
