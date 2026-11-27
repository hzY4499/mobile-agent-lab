import os
import re
import logging
import time
import base64
import uuid
from datetime import datetime
from typing import Any, Dict, Optional

from app.runtime_context import RuntimeContext
from device.device_manager import DeviceManager
from policy.base_policy import BasePolicy
from utils.pickle_utils import gzip_pickle
from utils import step_timing

# Policy type -> (Policy class, Processor class) registry
# Register new policies here.
POLICY_REGISTRY = {
    "ui_venus": {
        "policy": "policy.ui_venus_policy.UIVenusPolicy",
        "processor": "processor.uivenus_processor.UIVenusProcessor",
    },
    "ui_venus_2": {
        "policy": "policy.ui_venus_2_policy.UIVenus2Policy",
        "processor": "processor.ui_venus_2_processor.UIVenus2Processor",
    },
    # Example: add another policy.
    # "other_policy": {
    #     "policy": "policy.other_policy.OtherPolicy",
    #     "processor": "processor.other_processor.OtherProcessor",
    # },
}


class RunHandler:
    """Coordinate device control, policy execution, and trajectory storage."""
    
    def __init__(
        self,
        device_id: str,
        trace_dir: str,
        policy_type: str = "ui_venus_2",
        ep_config: Dict[str, Any] = None,
        app_mapping: Optional[Dict[str, str]] = None,
        **policy_kwargs
    ):
        """Initialize the run handler.
        
        Args:
            device_id: Device ID.
            trace_dir: Trajectory output directory.
            policy_type: Policy type whose processor is selected automatically.
            ep_config: Episode configuration.
            app_mapping: Application name mapping.
            **policy_kwargs: Additional policy arguments.
        """
        self.logger = logging.getLogger(__name__)
        self.runtime_context = RuntimeContext()
        self.step_limit = ep_config.get('step_limit')
        self.ep_config = ep_config
        self.final_answer = ""
        self._active_episode_dir = None
        self._partial_episode_data = []
        self.purpose = ""
        self.app_mapping: Dict[str, str] = app_mapping or {}
        self.device_manager = DeviceManager()
        self.device_id = device_id
        
        if not self.device_manager.connect_device(device_id):
            raise ConnectionError(f"设备连接失败: {device_id}")

        # Create the policy and its registered processor.
        self.policy, self.state_processor = self._create_policy_and_processor(policy_type, **policy_kwargs)
        self.trace_dir = trace_dir
        self.reflection_supervisor = None
        reflection_config = self.ep_config.get('reflection')
        if reflection_config:
            from verify.reflection_supervisor import ReflectionSupervisor
            self.reflection_supervisor = ReflectionSupervisor(**reflection_config.get('params', {}))
        
    def _create_policy_and_processor(self, policy_type: str, **kwargs) -> tuple:
        """Create the policy and processor for the selected policy type.
        
        POLICY_REGISTRY binds each policy to its processor. Register a new
        policy in that mapping to make it available.
        """
        if policy_type not in POLICY_REGISTRY:
            available = ", ".join(POLICY_REGISTRY.keys())
            raise ValueError(f"不支持的策略类型: {policy_type}，可用类型: {available}")
        
        registry = POLICY_REGISTRY[policy_type]
        
        # Import the policy class dynamically.
        policy_module, policy_class = registry["policy"].rsplit(".", 1)
        PolicyClass = getattr(__import__(policy_module, fromlist=[policy_class]), policy_class)
        
        # Import the processor class dynamically.
        processor_module, processor_class = registry["processor"].rsplit(".", 1)
        ProcessorClass = getattr(__import__(processor_module, fromlist=[processor_class]), processor_class)
        
        processor_kwargs = {}
        if policy_type == "ui_venus_2":
            processor_kwargs["n_img"] = kwargs.pop("n_img", 2)
        policy = PolicyClass(self.runtime_context, **kwargs)
        processor = ProcessorClass(**processor_kwargs)
        
        self.logger.info("已加载策略: %s, 处理器: %s", policy_class, processor_class)
        return policy, processor
            
    def run(self, purpose: str = '') -> tuple:
        """Run the main task loop.
        
        Args:
            purpose: Task objective.
            
        Returns:
            Whether the task completed successfully.
        """
        self.purpose = purpose
        self.final_answer = ""
        self._active_episode_dir = None
        self._partial_episode_data = []
        episode_start_time = time.time()
        
        # Build the trajectory directory name from the timestamp, task prefix, and short UUID.
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        # Remove punctuation and special characters, then keep the first ten task characters.
        task_desc = re.sub(r'[^\u4e00-\u9fa5a-zA-Z0-9]', '', purpose)[:10]
        short_id = str(uuid.uuid4())[:8]
        episode_id = f"{timestamp}_{task_desc}_{short_id}"
        
        # Create the trajectory directory for this run.
        episode_dir = os.path.join(self.trace_dir, episode_id)
        screenshots_dir = os.path.join(episode_dir, "screenshots")
        os.makedirs(screenshots_dir, exist_ok=True)
        # External recording can recover completed rows if this run is interrupted.
        self._active_episode_dir = episode_dir
        self._episode_started_at = episode_start_time
        
        # Trajectory data structure.
        episode_data = []
        self._partial_episode_data = episode_data
        is_successful = False
        call_user_content = None
        ep_end = False
        termination_reason = "max_steps"  # Default reason: the maximum number of steps was reached.
        
        step_timings = []

        # Repeated-action detection.
        recent_actions = []  # Store recent actions for repeated-action detection.
        max_repeat = 5  # Maximum allowed repetitions.
        
        # Detect the app associated with the task.
        mini_app_name = self.ep_config.get('mini_app_name', 'Android App')
        for app_name in self.app_mapping.keys():
            if app_name in purpose:
                mini_app_name = app_name
                break
        
        while self.runtime_context.step < self.step_limit:
            step_start_time = time.time()
            step_start_mono = step_timing.now()
            self.runtime_context.step += 1
            step_num = self.runtime_context.step
            self.logger.info("当前步数: %d", step_num)
            
            # 1. Capture a screenshot.
            timing = {'step': step_num}
            self._reset_screenshot_stats()
            segment_start = step_timing.now()
            screenshot_data = self.req_from_client()
            timing['screenshot_s'] = step_timing.now() - segment_start
            timing.update(self._collect_screenshot_stats())
            if screenshot_data.get('ep_end'):
                timing['step_total_s'] = step_timing.now() - step_start_mono
                self.logger.info("截图获取失败，任务结束")
                self.logger.info(step_timing.format_step_log(timing))
                ep_end = True
                termination_reason = "screenshot_failed"
                break
                
            screenshot_data.update({'purpose': self.purpose})
            
            # 2. Process the state and obtain an action.
            policy_recorder = self._call_recorder(self.policy)
            if policy_recorder is not None:
                policy_recorder.reset()
            segment_start = step_timing.now()
            state, action, pred_action, raw_response, think, conclusion = self.handle_client_req(screenshot_data)
            timing['model_s'] = step_timing.now() - segment_start
            if policy_recorder is not None:
                model_calls = policy_recorder.snapshot()
                timing['model_call_s'] = model_calls['duration_s']
                timing['model_calls'] = model_calls['calls']
                timing['model_attempts'] = model_calls['attempts']
                timing['model_http_requests'] = model_calls['http_requests']

            reflection_history = []
            segment_start = step_timing.now()
            judge_recorder = self._call_recorder(self.reflection_supervisor)
            if policy_recorder is not None:
                policy_recorder.reset()
            if judge_recorder is not None:
                judge_recorder.reset()
            if self.reflection_supervisor and action is not None:
                from verify.reflection_supervisor import build_feedback
                for retry_index in range(self.reflection_supervisor.max_retries):
                    judgment = self.reflection_supervisor.judge_action(
                        goal=self.purpose,
                        step=step_num,
                        action=action,
                        think=think,
                        screenshot_b64=screenshot_data['screenshot_str'],
                    )
                    verdict = judgment.get('verdict', 'CORRECT')
                    reflection_history.append({
                        'retry': retry_index,
                        'verdict': verdict,
                        'judgment': judgment,
                        'rejected_action': action,
                        'pred_action': pred_action,
                        'raw_response': raw_response,
                        'think': think,
                        'conclusion': conclusion,
                    })
                    self.logger.info("反思第 %d 次: verdict=%s", retry_index + 1, verdict)
                    if verdict in ('CORRECT', 'EXPLORATORY'):
                        break
                    action, pred_action, raw_response, think, conclusion = self.policy.retry_with_feedback(
                        build_feedback(judgment),
                        state,
                    )
                    if action is None:
                        break
                self.reflection_supervisor.notify_step_committed()
            if self.reflection_supervisor:
                timing['reflection_s'] = step_timing.now() - segment_start
                timing['reflection_rounds'] = len(reflection_history)
                self._add_reflection_timing(timing, judge_recorder, policy_recorder)
            
            # Print the model's reasoning.
            if think:
                self.logger.info("模型思考: %s", think.strip())
            
            # Detect repeated actions.
            action_signature = self._get_action_signature(action)
            recent_actions.append(action_signature)
            if len(recent_actions) > max_repeat:
                recent_actions.pop(0)
            
            # Treat N consecutive identical non-swipe actions as a loop.
            action_type = action.get('action_type') if action else None
            is_swipe = action_type and 'swipe' in action_type.lower()
            is_repeat_loop = len(recent_actions) >= max_repeat and len(set(recent_actions)) == 1 and not is_swipe
            if is_repeat_loop:
                self.logger.warning("检测到连续 %d 次重复动作: %s，任务陷入循环，终止执行", max_repeat, action_signature)
            
            # 3. Save the screenshot.
            screenshot_path = f"step_{step_num:03d}.png"
            segment_start = step_timing.now()
            self._save_screenshot(
                screenshot_data['screenshot_str'],
                os.path.join(screenshots_dir, screenshot_path)
            )
            timing['save_screenshot_s'] = step_timing.now() - segment_start
            
            # 4. Execute the action.
            success = False
            
            if is_repeat_loop:
                # Exit without executing another action after detecting a loop.
                ep_end = True
                termination_reason = "repeat_loop"
            elif action is None:
                self.logger.error("策略未返回有效动作")
                ep_end = True
                termination_reason = "policy_failed"
            elif action_type in ['CallUser', 'SUCCESS']:
                self.logger.info("任务完成")
                self.final_answer = action.get('input', '')
                is_successful = action_type == 'SUCCESS'
                if action_type == 'CallUser':
                    call_user_content = action.get('input', '')
                ep_end = True
                termination_reason = "success" if is_successful else "call_user"
            else:
                segment_start = step_timing.now()
                success = self.rsp_to_client(action)
                timing['action_s'] = step_timing.now() - segment_start
                segment_start = step_timing.now()
                time.sleep(2.5)
                timing['wait_s'] = step_timing.now() - segment_start
                
                # Update the history.
                history_item = {
                    'timestamp': time.time(),
                    'state': state,
                    'action': action,
                }
                self.runtime_context.history.append(history_item)
                self.runtime_context.update_action_description(state, action, pred_action)
                self.runtime_context.update_history('action_description', self.runtime_context.action_description[-1])
                self.policy.report_result(success)
            
            # 5. Record step data, storing only the screenshot path rather than raw image data.
            step_data = {
                'step': step_num,
                'screenshot_path': screenshot_path,
                'state': {k: v for k, v in state.items() if k != 'screenshot_str'},  # Exclude large data.
                'action': action,
                'pred_action': pred_action,
                'think': think,
                'conclusion': conclusion,
                'raw_response': raw_response,
                'reflection_history': reflection_history,
                'success': success,
                'timestamp': time.time(),
                'step_time': time.time() - step_start_time
            }
            step_timing_record = self._finish_step_timing(timing, step_start_mono)
            step_data['timing'] = step_timing_record
            step_timings.append(step_timing_record)
            episode_data.append(step_data)
            self.logger.info(step_timing.format_step_log(step_timing_record))
            
            if ep_end:
                break
        
        # A false ep_end here means the loop ended because it exceeded the step limit.
        if not ep_end:
            self.logger.warning("达到最大步数限制: %d", self.step_limit)
            termination_reason = "max_steps"
                
        # Calculate total elapsed time.
        run_time = time.time() - episode_start_time
        
        # Build the complete trajectory.
        trajectory = {
            "goal": purpose,
            "episode_id": episode_id,
            "device_id": self.device_id,
            "mini_app_name": mini_app_name,
            "episode_data": episode_data,
            "episode_length": self.runtime_context.step,
            "run_time": run_time,
            "is_successful": is_successful,
            "ep_end": ep_end,
            "termination_reason": termination_reason,
            "final_answer": self.final_answer,
            "timing_summary": step_timing.summarize(step_timings),
        }
        self.logger.info(step_timing.format_summary_log(trajectory["timing_summary"]))
        
        # Save the trajectory as pkl.gz.
        save_path = os.path.join(episode_dir, "trajectory.pkl.gz")
        try:
            compressed = gzip_pickle(trajectory)
            with open(save_path, 'wb') as f:
                f.write(compressed)
            self.logger.info("轨迹已保存: %s", save_path)
        except (OSError, ValueError) as e:
            self.logger.error("保存轨迹失败: %s", str(e))

        self.logger.info(
            "Episode 完成: episode_id=%s, steps=%d, run_time=%.2fs, is_successful=%s",
            episode_id, self.runtime_context.step, run_time, is_successful
        )

        self.reset()
        return is_successful, termination_reason, call_user_content

    @staticmethod
    def _call_recorder(owner):
        recorder = getattr(owner, 'call_recorder', None)
        return recorder if isinstance(recorder, step_timing.CallRecorder) else None

    def _reset_screenshot_stats(self):
        device = self.device_manager.get_device(self.device_id)
        if device is not None:
            try:
                device.last_screenshot_stats = None
            except AttributeError:
                pass

    def _collect_screenshot_stats(self) -> Dict[str, Any]:
        """Read the capture/encode breakdown recorded by the device, if it provides one."""
        device = self.device_manager.get_device(self.device_id)
        stats = getattr(device, 'last_screenshot_stats', None)
        if not isinstance(stats, dict):
            return {}
        result = {
            'screenshot_capture_s': stats.get('capture_s'),
            'screenshot_encode_s': stats.get('encode_s'),
            'screenshot_attempts': stats.get('attempts'),
            'screenshot_bytes': stats.get('bytes'),
        }
        if stats.get('width') and stats.get('height'):
            result['screenshot_width'] = stats['width']
            result['screenshot_height'] = stats['height']
        return {k: v for k, v in result.items() if v is not None}

    @staticmethod
    def _add_reflection_timing(timing, judge_recorder, retry_recorder):
        if judge_recorder is not None:
            judge = judge_recorder.snapshot()
            timing['reflection_judge_s'] = judge['duration_s']
            timing['reflection_judge_calls'] = judge['calls']
            timing['reflection_judge_attempts'] = judge['attempts']
            timing['reflection_judge_http_requests'] = judge['http_requests']
        if retry_recorder is not None:
            retry = retry_recorder.snapshot()
            timing['reflection_retry_model_s'] = retry['duration_s']
            timing['reflection_retry_calls'] = retry['calls']
            timing['reflection_retry_attempts'] = retry['attempts']
            timing['reflection_retry_http_requests'] = retry['http_requests']

    @staticmethod
    def _finish_step_timing(timing: Dict[str, Any], step_start_mono: float) -> Dict[str, Any]:
        """Close a step record. ``other_s`` is whatever the named segments do not cover."""
        total = step_timing.now() - step_start_mono
        named = sum(timing.get(k, 0.0) for k in (
            'screenshot_s', 'model_s', 'reflection_s', 'save_screenshot_s', 'action_s', 'wait_s'))
        timing['step_total_s'] = total
        timing['other_s'] = max(0.0, total - named)
        return timing

    def _save_screenshot(self, screenshot_str: str, filepath: str):
        """Save a screenshot.
        
        Args:
            screenshot_str: Base64-encoded screenshot.
            filepath: Output path.
        """
        try:
            img_data = base64.b64decode(screenshot_str)
            with open(filepath, "wb") as f:
                f.write(img_data)
        except (OSError, ValueError) as e:
            self.logger.error("保存截图失败: %s", str(e))

    def _get_action_signature(self, action: Optional[Dict[str, Any]]) -> str:
        """Build an action signature for repeated-action detection.
        
        Args:
            action: Action dictionary.
            
        Returns:
            String action signature.
        """
        if action is None:
            return "None"
        
        action_type = action.get('action_type', '')
        action_pos = action.get('action_pos', [])
        action_input = action.get('input', '')
        app_name = action.get('app_name', '')
        
        # Treat nearby tap coordinates as identical, with a 50-pixel tolerance.
        if action_type == 'CLK' and action_pos:
            x, y = action_pos[0]
            # Quantize coordinates to 50-pixel precision.
            x_q, y_q = x // 50 * 50, y // 50 * 50
            return f"{action_type}:{x_q},{y_q}"
        elif action_type == 'INPUT':
            return f"{action_type}:{action_input}"
        elif action_type == 'REOPEN':
            return f"{action_type}:{app_name}"
        else:
            return action_type

    def req_from_client(self) -> Dict[str, Any]:
        """Capture a screenshot from the device."""
        device = self.device_manager.get_device(self.device_id)
        screenshot = device.screenshot()
        if not screenshot:
            if self.runtime_context.step == 1:
                raise RuntimeError("首次获取截图失败")
            return {'screenshot_str': None, 'ep_end': True}
        return {'screenshot_str': screenshot, 'ep_end': False}
    
    def handle_client_req(self, req_data: Dict[str, Any]) -> tuple:
        """Process a request and obtain the next action."""
        self.logger.info("开始处理状态")
        state = self.state_processor.process(req_data, self.runtime_context.step, self.runtime_context.history)
        
        self.logger.info("请求策略决策")
        action, pred_action, parse_result, think, conclusion = self.policy.get_next_action(state)
        self.logger.info("策略输出: %s", action)
        return state, action, pred_action, parse_result, think, conclusion
    
    def rsp_to_client(self, rsp: Dict[str, Any]) -> bool:
        """Execute an action and return its result."""
        success = self._execute_action(rsp)
        if success:
            self.logger.info("操作成功: %s", rsp.get('action_type'))
        else:
            self.logger.error("操作失败: %s", rsp)
        return success
    
    def reset(self, go_home: bool = False):
        """Reset the runtime environment.
        
        Args:
            go_home: Whether to return to the home screen. Defaults to false.
        """
        if go_home:
            device = self.device_manager.get_device(self.device_id)
            if device:
                device.presshome()
        self.runtime_context.reset()
        self.state_processor.reset()
     
    def _execute_action(self, action: Dict[str, Any]) -> bool:
        """Execute an action.
        
        Args:
            action: Action dictionary.
            
        Returns:
            Whether execution succeeded.
        """
        try:
            device = self.device_manager.get_device(self.device_id)
            if not device:
                return False

            action_type = action["action_type"]
            positions = action.get("action_pos", [])
            
            if action_type == "CLK":
                return device.tap(positions[0][0], positions[0][1])
            elif action_type == "SWIPE":
                return device.swipe(positions[0][0], positions[0][1], 
                                   positions[1][0], positions[1][1], 
                                   action.get("duration", 1000))
            elif action_type == "INPUT":
                return device.input_text(action.get("input", ""))
            elif action_type == "BACK":
                return device.pressback()
            elif action_type == "WAIT":
                time.sleep(action.get("duration", 1000) / 1000)
                return True
            elif action_type == "PressHome":
                return device.presshome()
            elif action_type == "PressMenu":
                return device.pressmenu()
            elif action_type == "PressEnter":
                return device.pressenter()
            elif action_type == "LongPress":
                return device.longpress(positions[0][0], positions[0][1])
            elif action_type == "REOPEN":
                target = self.app_mapping.get(action.get("app_name"), action.get("app_name"))
                return device.launch_app(target)
            elif action_type == "DoubleCLK":
                return device.double_tap(positions[0][0], positions[0][1])
            elif action_type in ["SUCCESS", "FAIL", "CallUser"]:
                return True
            elif action_type == "get_screenshot":
                return device.get_screenshot_to_album()
            else:
                self.logger.error("不支持的操作类型: %s", action_type)
                return False
        except (IndexError, KeyError, TypeError) as e:
            self.logger.error("执行失败: %s", str(e))
            return False
            
