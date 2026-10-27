using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Numerics;
using System.Runtime.InteropServices;
using Windows.UI.Composition;
using Windows.UI.Composition.Desktop;

namespace CodexQuotaBackdrop {
    // All instance methods must run on the constructing GUI thread.
    public sealed class RoundedBackdrop : IDisposable {
        [StructLayout(LayoutKind.Sequential)] struct QueueOptions { public int size, threadType, apartment; }
        [StructLayout(LayoutKind.Sequential, CharSet=CharSet.Unicode)] struct WindowClass {
            public uint style; public WndProc proc; public int classExtra, windowExtra;
            public IntPtr instance, icon, cursor, background, menu;
            [MarshalAs(UnmanagedType.LPWStr)] public string name;
        }
        [StructLayout(LayoutKind.Sequential)] struct Accent { public int state, flags; public uint color; public int animation; }
        [StructLayout(LayoutKind.Sequential)] struct CompositionData { public int attribute; public IntPtr data; public UIntPtr size; }
        [ComImport, Guid("29E691FA-4567-4DCA-B319-D0F207EB6807"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
        interface ICompositorDesktopInterop {
            [PreserveSig] int CreateDesktopWindowTarget(IntPtr hwnd, [MarshalAs(UnmanagedType.Bool)] bool topmost, out IntPtr target);
            [PreserveSig] int EnsureOnThread(uint threadId);
        }
        delegate IntPtr WndProc(IntPtr hwnd, uint msg, IntPtr wp, IntPtr lp);
        static readonly WndProc Procedure = WindowProcedure;
        static readonly Dictionary<uint, IntPtr> Queues = new Dictionary<uint, IntPtr>();
        static readonly string ClassName = "CodexQuotaRoundedBackdrop_" + Guid.NewGuid().ToString("N");
        static ushort atom;
        readonly uint thread;
        readonly IntPtr owner;
        IntPtr hwnd;
        Compositor compositor;
        DesktopWindowTarget target;
        SpriteVisual visual;
        CompositionRoundedRectangleGeometry geometry;
        CompositionGeometricClip clip;
        CompositionBackdropBrush brush;
        bool disposed;
        bool hasSync;
        int lastX,lastY,lastWidth,lastHeight;
        float lastRadius;
        bool lastVisible;

        public IntPtr Handle { get { return hwnd; } }
        public RoundedBackdrop(IntPtr owner) {
            this.thread = GetCurrentThreadId(); this.owner = owner;
            if (!IsWindow(owner)) throw new ArgumentException("HUD owner HWND is invalid");
            uint pid; uint ownerThread = GetWindowThreadProcessId(owner, out pid);
            if (ownerThread != thread || pid != GetCurrentProcessId())
                throw new ArgumentException("HUD owner must belong to the current GUI thread and process");
            try {
                lock (Queues) {
                    if (!Queues.ContainsKey(thread)) {
                        IntPtr controller;
                        Marshal.ThrowExceptionForHR(CreateDispatcherQueueController(new QueueOptions { size=12, threadType=2, apartment=2 }, out controller));
                        if (controller == IntPtr.Zero) throw new InvalidOperationException("No dispatcher queue controller");
                        // Current-thread queues are kept alive for the GUI thread lifetime.
                        Queues.Add(thread, controller);
                    }
                    if (atom == 0) {
                        WindowClass wc = new WindowClass { proc=Procedure, instance=GetModuleHandle(null), name=ClassName };
                        atom=RegisterClassW(ref wc);
                        if (atom==0) throw new Win32Exception(Marshal.GetLastWin32Error());
                    }
                }
                // No owner: an owned popup would always be above the Qt root.
                hwnd=CreateWindowExW(0x082000A0, ClassName, "Codex rounded backdrop", 0x80000000,
                                    0,0,1,1,IntPtr.Zero,IntPtr.Zero,GetModuleHandle(null),IntPtr.Zero);
                if (hwnd==IntPtr.Zero) throw new Win32Exception(Marshal.GetLastWin32Error());
                Accent accent=new Accent { state=5 };
                IntPtr memory=Marshal.AllocHGlobal(Marshal.SizeOf(typeof(Accent)));
                try {
                    Marshal.StructureToPtr(accent,memory,false);
                    CompositionData data=new CompositionData { attribute=19, data=memory, size=(UIntPtr)Marshal.SizeOf(typeof(Accent)) };
                    if (!SetWindowCompositionAttribute(hwnd,ref data)) throw new Win32Exception(Marshal.GetLastWin32Error(),"Host backdrop Accent5 rejected");
                } finally { Marshal.FreeHGlobal(memory); }
                compositor=new Compositor();
                IntPtr raw;
                Marshal.ThrowExceptionForHR(((ICompositorDesktopInterop)(object)compositor).CreateDesktopWindowTarget(hwnd,false,out raw));
                if(raw==IntPtr.Zero) throw new InvalidOperationException("No composition target");
                try { target=(DesktopWindowTarget)Marshal.GetObjectForIUnknown(raw); }
                finally { Marshal.Release(raw); }
                visual=compositor.CreateSpriteVisual();
                geometry=compositor.CreateRoundedRectangleGeometry();
                clip=compositor.CreateGeometricClip(geometry);
                brush=compositor.CreateHostBackdropBrush();
                visual.Brush=brush; visual.Clip=clip; target.Root=visual;
            } catch { Dispose(); throw; }
        }
        public void Sync(int x,int y,int width,int height,float radius,bool topmost,bool visible) {
            CheckThread();
            if(disposed) throw new ObjectDisposedException("RoundedBackdrop");
            if(!IsWindow(owner)) { ShowWindow(hwnd,0); throw new InvalidOperationException("HUD owner destroyed"); }
            if(width<=0 || height<=0 || float.IsNaN(radius) || float.IsInfinity(radius) || radius<0)
                throw new ArgumentException("Invalid backdrop geometry");
            bool currentTopmost=(GetWindowLongPtrW(hwnd,-20).ToInt64() & 8)!=0;
            if(hasSync && x==lastX && y==lastY && width==lastWidth && height==lastHeight && radius==lastRadius &&
               visible==lastVisible && IsWindowVisible(hwnd)==visible && currentTopmost==topmost && GetWindow(owner,2)==hwnd)
                return;
            visual.Size=new Vector2(width,height);
            geometry.Size=new Vector2(width,height);
            geometry.CornerRadius=new Vector2(Math.Min(radius,Math.Min(width,height)/2f));
            if(!visible) ShowWindow(hwnd,0);
            // Never activate. Establish the requested Z band, then immediately below owner.
            if(currentTopmost!=topmost && !SetWindowPos(hwnd,topmost ? new IntPtr(-1) : new IntPtr(-2),x,y,width,height,0x10))
                throw new Win32Exception(Marshal.GetLastWin32Error());
            if(!SetWindowPos(hwnd,owner,x,y,width,height,(uint)(0x10 | (visible ? 0x40 : 0))))
                throw new Win32Exception(Marshal.GetLastWin32Error());
            hasSync=true; lastX=x; lastY=y; lastWidth=width; lastHeight=height; lastRadius=radius; lastVisible=visible;
        }
        void CheckThread() { if(GetCurrentThreadId()!=thread) throw new InvalidOperationException("Backdrop requires its GUI thread"); }
        public void Dispose() {
            CheckThread(); if(disposed)return; disposed=true;
            if(hwnd!=IntPtr.Zero) ShowWindow(hwnd,0);
            // Dispose each resource even if an earlier WinRT close fails.
            try { if(target!=null) target.Root=null; } catch { }
            Close(visual); Close(clip); Close(geometry); Close(brush); Close(target); Close(compositor);
            visual=null; clip=null; geometry=null; brush=null; target=null; compositor=null;
            if(hwnd!=IntPtr.Zero) { DestroyWindow(hwnd); hwnd=IntPtr.Zero; }
        }
        static void Close(object value) { IDisposable close=value as IDisposable; if(close!=null)try {close.Dispose();}catch{} }
        static IntPtr WindowProcedure(IntPtr hwnd,uint msg,IntPtr wp,IntPtr lp) {
            if(msg==0x84) return new IntPtr(-1); // HTTRANSPARENT
            if(msg==0x21) return new IntPtr(3); // MA_NOACTIVATE
            return DefWindowProcW(hwnd,msg,wp,lp);
        }
        [DllImport("CoreMessaging.dll")] static extern int CreateDispatcherQueueController(QueueOptions options,out IntPtr controller);
        [DllImport("kernel32.dll")] static extern uint GetCurrentThreadId();
        [DllImport("kernel32.dll")] static extern uint GetCurrentProcessId();
        [DllImport("kernel32.dll",CharSet=CharSet.Unicode)] static extern IntPtr GetModuleHandle(string name);
        [DllImport("user32.dll",SetLastError=true)] static extern ushort RegisterClassW(ref WindowClass wc);
        [DllImport("user32.dll",CharSet=CharSet.Unicode,SetLastError=true)] static extern IntPtr CreateWindowExW(uint ex,string cls,string title,uint style,int x,int y,int w,int h,IntPtr parent,IntPtr menu,IntPtr instance,IntPtr param);
        [DllImport("user32.dll")] static extern IntPtr DefWindowProcW(IntPtr hwnd,uint msg,IntPtr wp,IntPtr lp);
        [DllImport("user32.dll")] static extern bool IsWindow(IntPtr hwnd);
        [DllImport("user32.dll")] static extern bool IsWindowVisible(IntPtr hwnd);
        [DllImport("user32.dll")] static extern IntPtr GetWindow(IntPtr hwnd,uint command);
        [DllImport("user32.dll",SetLastError=true)] static extern IntPtr GetWindowLongPtrW(IntPtr hwnd,int index);
        [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr hwnd,out uint pid);
        [DllImport("user32.dll")] static extern bool ShowWindow(IntPtr hwnd,int command);
        [DllImport("user32.dll",SetLastError=true)] static extern bool DestroyWindow(IntPtr hwnd);
        [DllImport("user32.dll",SetLastError=true)] static extern bool SetWindowPos(IntPtr hwnd,IntPtr after,int x,int y,int w,int h,uint flags);
        [DllImport("user32.dll",SetLastError=true)] static extern bool SetWindowCompositionAttribute(IntPtr hwnd,ref CompositionData data);
    }
}
