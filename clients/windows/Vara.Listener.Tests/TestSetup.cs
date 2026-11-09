using System.Runtime.CompilerServices;

namespace Vara.Listener.Tests;

internal static class TestSetup
{
    [ModuleInitializer]
    internal static void DisableLogging() => Log.Enabled = false;
}
