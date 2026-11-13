param(
    [Parameter(Mandatory = $true)][string]$Profile
)

# 用 wlanapi.dll 的 WlanConnect 连接“已保存的”无线配置文件。
# 不走 netsh：netsh wlan connect 会先枚举网络列表，那一步需要管理员+定位权限，
# 而直接按 profile 连接不需要。

Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;

public static class WifiNative
{
    [StructLayout(LayoutKind.Sequential)]
    public struct WLAN_INTERFACE_INFO
    {
        public Guid InterfaceGuid;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 256)]
        public string strInterfaceDescription;
        public int isState;
    }

    [StructLayout(LayoutKind.Sequential)]
    public struct WLAN_CONNECTION_PARAMETERS
    {
        public int wlanConnectionMode;
        [MarshalAs(UnmanagedType.LPWStr)] public string strProfile;
        public IntPtr pDot11Ssid;
        public IntPtr pDesiredBssidList;
        public int dot11BssType;
        public int dwFlags;
    }

    [DllImport("wlanapi.dll")]
    public static extern int WlanOpenHandle(uint dwClientVersion, IntPtr pReserved,
        out uint pdwNegotiatedVersion, out IntPtr phClientHandle);

    [DllImport("wlanapi.dll")]
    public static extern int WlanCloseHandle(IntPtr hClientHandle, IntPtr pReserved);

    [DllImport("wlanapi.dll")]
    public static extern int WlanEnumInterfaces(IntPtr hClientHandle, IntPtr pReserved,
        out IntPtr ppInterfaceList);

    [DllImport("wlanapi.dll")]
    public static extern int WlanConnect(IntPtr hClientHandle, ref Guid pInterfaceGuid,
        ref WLAN_CONNECTION_PARAMETERS pConnectionParameters, IntPtr pReserved);

    [DllImport("wlanapi.dll")]
    public static extern void WlanFreeMemory(IntPtr pMemory);

    public static Guid[] Interfaces(out string[] descriptions)
    {
        IntPtr handle, list;
        uint negotiated;
        if (WlanOpenHandle(2, IntPtr.Zero, out negotiated, out handle) != 0)
            throw new Exception("WlanOpenHandle failed");
        try
        {
            if (WlanEnumInterfaces(handle, IntPtr.Zero, out list) != 0)
                throw new Exception("WlanEnumInterfaces failed");
            try
            {
                int count = Marshal.ReadInt32(list);
                int offset = 8; // dwNumberOfItems + dwIndex
                int size = Marshal.SizeOf(typeof(WLAN_INTERFACE_INFO));
                var guids = new Guid[count];
                var descs = new string[count];
                for (int i = 0; i < count; i++)
                {
                    var info = (WLAN_INTERFACE_INFO)Marshal.PtrToStructure(
                        new IntPtr(list.ToInt64() + offset + i * size), typeof(WLAN_INTERFACE_INFO));
                    guids[i] = info.InterfaceGuid;
                    descs[i] = info.strInterfaceDescription;
                }
                descriptions = descs;
                return guids;
            }
            finally { WlanFreeMemory(list); }
        }
        finally { WlanCloseHandle(handle, IntPtr.Zero); }
    }

    public static int Connect(Guid ifaceGuid, string profile)
    {
        IntPtr handle;
        uint negotiated;
        if (WlanOpenHandle(2, IntPtr.Zero, out negotiated, out handle) != 0)
            throw new Exception("WlanOpenHandle failed");
        try
        {
            var prm = new WLAN_CONNECTION_PARAMETERS();
            prm.wlanConnectionMode = 0;   // wlan_connection_mode_profile
            prm.strProfile = profile;
            prm.pDot11Ssid = IntPtr.Zero;
            prm.pDesiredBssidList = IntPtr.Zero;
            prm.dot11BssType = 1;         // infrastructure
            prm.dwFlags = 0;
            return WlanConnect(handle, ref ifaceGuid, ref prm, IntPtr.Zero);
        }
        finally { WlanCloseHandle(handle, IntPtr.Zero); }
    }
}
'@

$descs = $null
$guids = [WifiNative]::Interfaces([ref]$descs)
if ($guids.Count -eq 0) { throw "没有找到无线网卡" }
Write-Host ("网卡: " + ($descs -join ", "))

$rc = [WifiNative]::Connect($guids[0], $Profile)
if ($rc -ne 0) { throw ("WlanConnect 失败，错误码 " + $rc + "（1168=找不到配置文件，1062=服务未启动）") }
Write-Host "已发起连接：$Profile"
