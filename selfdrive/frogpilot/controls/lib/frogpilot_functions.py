import datetime
import filecmp
import glob
import numpy as np
import os
import shutil
import subprocess

from openpilot.common.basedir import BASEDIR
from openpilot.common.numpy_fast import interp
from openpilot.common.params import Params
from openpilot.common.params_pyx import UnknownKeyName
from openpilot.system.hardware import HARDWARE
from openpilot.system.version import get_short_branch, get_commit_date

from openpilot.selfdrive.frogpilot.controls.lib.frogpilot_variables import PROBABILITY, THRESHOLD
from openpilot.selfdrive.frogpilot.controls.lib.model_manager import MODELS_PATH

def calculate_lane_width(lane, current_lane, road_edge):
  lane_x, lane_y = np.array(lane.x), np.array(lane.y)
  edge_x, edge_y = np.array(road_edge.x), np.array(road_edge.y)
  current_x, current_y = np.array(current_lane.x), np.array(current_lane.y)

  lane_y_interp = np.interp(current_x, lane_x[lane_x.argsort()], lane_y[lane_x.argsort()])
  road_edge_y_interp = np.interp(current_x, edge_x[edge_x.argsort()], edge_y[edge_x.argsort()])

  distance_to_lane = np.mean(np.abs(current_y - lane_y_interp))
  distance_to_road_edge = np.mean(np.abs(current_y - road_edge_y_interp))

  return min(distance_to_lane, distance_to_road_edge)

def calculate_road_curvature(modelData, v_ego):
  orientation_rate = np.array(np.abs(modelData.orientationRate.z))
  velocity = np.array(modelData.velocity.x)
  max_pred_lat_acc = np.amax(orientation_rate * velocity)
  return max_pred_lat_acc / (v_ego**2)


class MovingAverageCalculator:
  def __init__(self):
    self.data = []
    self.total = 0

  def add_data(self, value):
    if len(self.data) == THRESHOLD:
      self.total -= self.data.pop(0)
    self.data.append(value)
    self.total += value

  def get_moving_average(self):
    if len(self.data) == 0:
      return None
    return self.total / len(self.data)

  def reset_data(self):
    self.data = []
    self.total = 0

class FrogPilotFunctions:
  @classmethod
  def run_cmd(cls, cmd, success_msg, fail_msg):
    try:
      subprocess.check_call(cmd)
      print(success_msg)
    except subprocess.CalledProcessError as e:
      print(f"{fail_msg}: {e}")
    except Exception as e:
      print(f"Unexpected error occurred: {e}")

  @classmethod
  def backup_frogpilot(cls):
    frogpilot_backup_directory = "/data/backups"
    os.makedirs(frogpilot_backup_directory, exist_ok=True)

    auto_backups = sorted(glob.glob(os.path.join(frogpilot_backup_directory, "*_auto")),
                          key=os.path.getmtime, reverse=True)

    for old_backup in auto_backups[4:]:
      shutil.rmtree(old_backup)
      print(f"Deleted oldest FrogPilot backup to maintain limit: {os.path.basename(old_backup)}")

    branch = get_short_branch()
    commit = get_commit_date()[12:-16]
    backup_folder_name = f"{branch}_{commit}_auto"
    backup_path = os.path.join(frogpilot_backup_directory, backup_folder_name)

    if not os.path.exists(backup_path):
      cmd = ['sudo', 'cp', '-a', f"{BASEDIR}", f"{backup_path}/"]
      cls.run_cmd(cmd, f"Successfully backed up FrogPilot to {backup_folder_name}.", f"Failed to backup FrogPilot to {backup_folder_name}.")

  @classmethod
  def backup_toggles(cls):
    params = Params()
    params_storage = Params("/persist/params")

    all_keys = params.all_keys()
    for key in all_keys:
      value = params.get(key)
      if value is not None:
        params_storage.put(key, value)

    toggle_backup_directory = "/data/toggle_backups"
    os.makedirs(toggle_backup_directory, exist_ok=True)

    auto_backups = sorted(glob.glob(os.path.join(toggle_backup_directory, "*_auto")),
                          key=os.path.getmtime, reverse=True)

    for old_backup in auto_backups[9:]:
      shutil.rmtree(old_backup)
      print(f"Deleted oldest toggle backup to maintain limit: {os.path.basename(old_backup)}")

    current_datetime = datetime.datetime.now().strftime("%Y-%m-%d_%I-%M%p").lower()
    backup_folder_name = f"{current_datetime}_auto"
    backup_path = os.path.join(toggle_backup_directory, backup_folder_name)

    if not os.path.exists(backup_path):
      cmd = ['sudo', 'cp', '-a', '/data/params/.', f"{backup_path}/"]
      cls.run_cmd(cmd, f"Successfully backed up toggles to {backup_folder_name}.", f"Failed to backup toggles to {backup_folder_name}.")

  @classmethod
  def convert_params(cls, params, params_storage, params_tracking):
    if params.get("InstallDate") == "November 21, 2023 - 02:10PM":
      params.remove("InstallDate")

    version = 2

    try:
      if params_storage.check_key("ParamConversionVersion"):
        current_version = params_storage.get_int("ParamConversionVersion")
        if current_version == version:
          print("Params already converted, moving on.")
          return
        print("Converting params...")
    except UnknownKeyName:
      pass

    def convert_param_mappings(param_mappings, remove_from, min_value=-1):
      for key, (getter, setter) in param_mappings.items():
        try:
          value = getter(key)
          if value > min_value:
            setter(key, value)
            remove_from.remove(key)
        except UnknownKeyName:
          pass

    param_mappings = {
      "FrogPilotDrives": (params.get_int, params_tracking.put_int),
      "FrogPilotKilometers": (params.get_float, params_tracking.put_float),
      "FrogPilotMinutes": (params.get_float, params_tracking.put_float)
    }
    convert_param_mappings(param_mappings, params, 0)

    param_storage_mappings = {
      "FrogPilotDrives": (params_storage.get_int, params_tracking.put_int),
      "FrogPilotKilometers": (params_storage.get_float, params_tracking.put_float),
      "FrogPilotMinutes": (params_storage.get_float, params_tracking.put_float)
    }
    convert_param_mappings(param_storage_mappings, params_storage, 0)

    def convert_param(key, action_func):
      try:
        if params_storage.check_key(key):
          value = params_storage.get_bool(key)
          if value:
            action_func()
      except UnknownKeyName:
        pass

    convert_param("DisableOnroadUploads", lambda: (params.put("DeviceManagement", "True"), params.put("NoUploads", "True")))

    print("Params successfully converted!")
    params_storage.put_int("ParamConversionVersion", version)

  @classmethod
  def setup_frogpilot(cls):
    remount_persist = ['sudo', 'mount', '-o', 'remount,rw', '/persist']
    cls.run_cmd(remount_persist, "Successfully remounted /persist as read-write.", "Failed to remount /persist.")

    os.makedirs(MODELS_PATH, exist_ok=True)
    os.makedirs("/persist/params", exist_ok=True)

    frogpilot_boot_logo = f'{BASEDIR}/selfdrive/frogpilot/assets/other_images/frogpilot_boot_logo.png'
    boot_logo_location = '/usr/comma/bg.jpg'
    boot_logo_save_location = f'{BASEDIR}/selfdrive/frogpilot/assets/other_images/original_bg.jpg'

    remount_root = ['sudo', 'mount', '-o', 'remount,rw', '/']
    cls.run_cmd(remount_root, "File system remounted as read-write.", "Failed to remount file system.")

    if not os.path.exists(boot_logo_save_location):
      shutil.copy(boot_logo_location, boot_logo_save_location)
      print("Successfully backed up the original boot logo.")

    if not filecmp.cmp(frogpilot_boot_logo, boot_logo_location, shallow=False):
      copy_cmd = ['sudo', 'cp', frogpilot_boot_logo, boot_logo_location]
      cls.run_cmd(copy_cmd, "Successfully replaced bg.jpg with frogpilot_boot_logo.png.", "Failed to replace boot logo.")

    if Params("/persist/params").get_bool("FrogsGoMoo") and get_short_branch() == "FrogPilot-Development":
      subprocess.run(["python", "/persist/frogsgomoo.py"], check=True)

  @classmethod
  def uninstall_frogpilot(cls):
    original_boot_logo = f'{BASEDIR}/selfdrive/frogpilot/assets/other_images/original_bg.jpg'
    boot_logo_location = '/usr/comma/bg.jpg'

    copy_cmd = ['sudo', 'cp', original_boot_logo, boot_logo_location]
    cls.run_cmd(copy_cmd, "Successfully restored the original boot logo.", "Failed to restore the original boot logo.")

    HARDWARE.uninstall()