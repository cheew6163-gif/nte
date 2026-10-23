import ctypes as C
import threading
import unittest
from unittest.mock import patch
from yihuan.windows import Actor, WinApi, INPUT, MOUSEINPUT, GameWindow, WinApiError, ActionChanged, Cancelled
from yihuan.core import Action, Intent, Phase, Scene
from test_input import FakeApi, StopOnWait


class NativeDiagnosticsTests(unittest.TestCase):
    def make(self, user):
        api = object.__new__(WinApi)
        api.user = user
        return api

    def test_physical_failure_keeps_error_and_actual_parameters(self):
        api = self.make(type('User', (), {'SetPhysicalCursorPos': lambda self,x,y: 0})())
        with patch('yihuan.windows.last_error', return_value=5):
            with self.assertRaises(WinApiError) as caught:
                api.set_physical_position(3379,2028)
        error=caught.exception.details
        self.assertEqual(error['api'],'SetPhysicalCursorPos')
        self.assertEqual(error['last_error'],5)
        self.assertEqual(error['parameters'],{'x':3379,'y':2028})
        self.assertEqual(error['return_value'],0)
        with patch('yihuan.windows.last_error',return_value=87): pass
        self.assertEqual(caught.exception.details['last_error'],5)

    def test_zero_error_failure_is_not_reported_as_success(self):
        api=self.make(type('User',(),{'GetPhysicalCursorPos': lambda self,p: 0})())
        with patch('yihuan.windows.last_error',return_value=0):
            with self.assertRaisesRegex(WinApiError,'未提供扩展错误码') as caught: api.cursor_position()
        self.assertEqual(caught.exception.details['last_error'],0)

    def test_sendinput_retains_entire_input_and_structure_size(self):
        api=self.make(type('User',(),{'SendInput': lambda self,n,p,size: 0})())
        item=INPUT(type=0);item.mi=MOUSEINPUT(60824,61559,0,0xC001,0,0)
        with patch('yihuan.windows.last_error',return_value=5):
            with self.assertRaises(WinApiError) as caught: api.send(item)
        args=caught.exception.details['parameters']
        self.assertEqual((args['dx'],args['dy'],args['dwFlags']),(60824,61559,0xC001))
        self.assertEqual(args['cbSize'],C.sizeof(INPUT));self.assertEqual(args['input_count'],1)

    def test_monitor_union_wins_over_dpi_virtualized_metrics(self):
        class User:
            EnumDisplayMonitors=True
            def GetSystemMetrics(self,i): return {76:-1707,77:0,78:4267,79:1440}[i]
        api=self.make(User())
        api.monitors=lambda:[{'left':-2560,'top':235,'width':2560,'height':1600},
                             {'left':0,'top':0,'width':3840,'height':2160}]
        self.assertEqual(api.absolute_position(3379,2028),(60824,61559))
        self.assertEqual(api.last_virtual_desktop['width'],6400)
        self.assertEqual(api.last_virtual_desktop['metrics']['width'],4267)

    def test_invalid_desktop_outside_target_and_screen_hole_are_rejected(self):
        class User:
            def GetSystemMetrics(self,i): return {76:0,77:0,78:1,79:1}[i]
        api=self.make(User())
        with self.assertRaisesRegex(RuntimeError,'范围无效'): api.absolute_position(0,0)
        api.virtual_desktop=lambda:{'left':-100,'top':0,'right':100,'bottom':100,'width':200,'height':100,
            'monitors':[{'left':-100,'top':0,'width':50,'height':100},{'left':0,'top':0,'width':100,'height':100}]}
        with self.assertRaisesRegex(RuntimeError,'超出虚拟桌面'): api.absolute_position(100,2)
        with self.assertRaisesRegex(RuntimeError,'空白区域'): api.absolute_position(-30,2)
        with self.assertRaisesRegex(RuntimeError,'有限整数'): api.absolute_position(float('nan'),2)


    def test_partial_foreground_failure_preserves_other_native_diagnostics(self):
        from yihuan.native_diagnostics import input_diagnostics
        class Api:
            user=type('User',(),{'GetForegroundWindow':lambda self:10})()
            def title(self,hwnd):raise RuntimeError('title failed')
            def process_id(self,hwnd):return 42
            def class_name(self,hwnd):return 'Game'
            def current_dpi(self):return {'per_monitor_v2':True}
            def virtual_desktop(self):return {'left':0,'top':0,'right':3840,'bottom':2160}
            def cursor_position(self):return (10,20)
            logical_cursor_position=cursor_position
            def clip_rectangle(self):return {'left':0,'top':0,'right':3840,'bottom':2160}
        with patch('yihuan.native_diagnostics.desktop_context',return_value={'same_input_desktop':True}), \
             patch('yihuan.native_diagnostics.token_status',return_value={'integrity_rid':8192}):
            result=input_diagnostics(Api())
        self.assertEqual(result['foreground']['pid'],42)
        self.assertIn('title failed',result['foreground']['title']['error'])
        self.assertEqual(result['physical_cursor'],[10,20])
        self.assertTrue(result['thread_dpi']['per_monitor_v2'])


class PositionGuardTests(unittest.TestCase):
    window=GameWindow(1,'异环',0,0,3840,2160)
    go=Intent(Action.GO,Scene.ACTIVITY,Phase.READY)

    def test_single_step_position_never_presses_left(self):
        api=FakeApi();actor=Actor(api,self.window,threading.Event())
        self.assertEqual(actor.position(3379,2028),(3379,2028))
        self.assertEqual(api.sent,[('mouse',0xC001)])
        self.assertFalse(actor.last_input['mouse_down_sent'])
        self.assertEqual(len(actor.last_input['position_samples'][0]['samples']),4)
        self.assertEqual(actor.last_input['cursor_before'],[0,0])

    def test_reference_edge_rounding_is_rejected(self):
        api=FakeApi();actor=Actor(api,self.window,threading.Event())
        with self.assertRaisesRegex(RuntimeError,'取整后超出'): actor.perform(self.go,{'go':(1498.99,842.99)})
        self.assertEqual(api.sent,[])

    def test_cursor_return_after_arrival_is_diagnosed(self):
        class Recenter(FakeApi):
            def __init__(self): super().__init__();self.reads=0
            def cursor_position(self):
                self.reads+=1
                if self.reads in (4,5): return (1261,449)
                return self.cursor
        api=Recenter();actor=Actor(api,self.window,threading.Event())
        with self.assertLogs('yihuan',level='WARNING'): actor.position(3379,2028)
        self.assertTrue(actor.last_input['cursor_returned_after_arrival'])
        self.assertEqual(api.corrections,[(3379,2028)])
        self.assertEqual(api.sent,[('mouse',0xC001)])

    def test_failure_stage_retains_physical_api_error(self):
        class Fail(FakeApi):
            def set_physical_position(self,x,y):
                raise WinApiError('Windows未接受物理坐标定位',{'api':'SetPhysicalCursorPos','parameters':{'x':x,'y':y},
                    'last_error':5,'error_text':'access denied','return_value':0})
        api=Fail(cursor_offset=(-1000,-400));actor=Actor(api,self.window,threading.Event())
        with self.assertLogs('yihuan'):
            with self.assertRaises(WinApiError): actor.position(3379,2028)
        self.assertIn('SetPhysicalCursorPos',actor.last_input['stage'])
        self.assertEqual(actor.last_input['windows_error']['last_error'],5)
        self.assertEqual(api.sent,[('mouse',0xC001)])

    def test_mismatched_input_desktop_and_clip_block_before_move(self):
        for context in ({'desktop':{'same_input_desktop':False}}, {'target_inside_clip':False}, {'uipi_risk':True}):
            api=FakeApi();api.input_diagnostics=lambda *args:context
            actor=Actor(api,self.window,threading.Event())
            with self.assertLogs('yihuan',level='ERROR'):
                with self.assertRaises(RuntimeError):actor.position(3379,2028)
            self.assertEqual(api.sent,[])

    def test_stop_or_focus_change_during_diagnostics_prevents_move(self):
        for reason in ('stop','focus'):
            stop=threading.Event();api=FakeApi()
            def context(*args):
                if reason=='stop':stop.set()
                else:api.focus=False
                return {}
            api.input_diagnostics=context
            actor=Actor(api,self.window,stop)
            with self.assertRaises((Cancelled,RuntimeError)):
                actor.position(3379,2028)
            self.assertEqual(api.sent,[])

    def test_recenter_during_final_diagnostics_cannot_report_position_verified(self):
        api=FakeApi();count=0
        def context(*args):
            nonlocal count
            count+=1
            if count==2:api.cursor=(1261,449)
            return {}
        api.input_diagnostics=context
        actor=Actor(api,self.window,threading.Event())
        with self.assertLogs('yihuan',level='ERROR'):
            with self.assertRaisesRegex(RuntimeError,'鼠标未到按钮'):actor.position(3379,2028)
        self.assertEqual(actor.last_input['cursor_final'],[1261,449])
        self.assertFalse(actor.last_input['mouse_down_sent'])

    def test_cursor_relocated_during_final_capture_prevents_down(self):
        api=FakeApi();actor=Actor(api,self.window,threading.Event())
        def guard(): api.cursor=(1261,449);return True
        with self.assertRaisesRegex(RuntimeError,'按下前鼠标位置改变'):
            actor.click_at(3379,2028,before_press=guard)
        self.assertEqual(api.sent,[('mouse',0xC001)])

    def test_clip_change_during_final_capture_prevents_down(self):
        api=FakeApi();api.clip_rectangle=lambda:{'left':0,'top':0,'right':100,'bottom':100}
        actor=Actor(api,self.window,threading.Event())
        with self.assertRaisesRegex(RuntimeError,'限制发生变化'): actor.click_at(3379,2028,before_press=lambda:True)
        self.assertEqual(api.sent,[('mouse',0xC001)])

    def test_focus_or_ctrl_change_during_final_capture_prevents_down(self):
        for field,value in [('focus',False),('ctrl',True)]:
            api=FakeApi();actor=Actor(api,self.window,threading.Event())
            def guard(): setattr(api,field,value);return True
            with self.assertRaises((RuntimeError,Cancelled)):actor.click_at(3379,2028,before_press=guard)
            self.assertEqual(api.sent,[('mouse',0xC001)])

    def test_action_changed_is_not_a_fatal_input_error(self):
        api=FakeApi();actor=Actor(api,self.window,threading.Event())
        with self.assertRaises(ActionChanged):actor.click_at(3379,2028,before_press=lambda:False)
        self.assertEqual(api.sent,[('mouse',0xC001)])

    def test_diagnostic_exception_cannot_hide_original_input_error(self):
        class Broken(FakeApi):
            def input_diagnostics(self,*args): raise RuntimeError('diagnostics broken')
            def send(self,item): raise WinApiError('native failure',{'api':'SendInput','last_error':5,'parameters':{},'return_value':0,'error_text':'denied'})
        actor=Actor(Broken(),self.window,threading.Event())
        with self.assertLogs('yihuan',level='ERROR'):
            with self.assertRaises(WinApiError) as caught:actor.position(3379,2028)
        self.assertEqual(caught.exception.details['api'],'SendInput')
        self.assertIn('diagnostics broken',actor.last_input['environment_failure']['diagnostic_error'])


if __name__=='__main__': unittest.main()
