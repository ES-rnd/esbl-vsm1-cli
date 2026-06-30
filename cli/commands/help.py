"""help command."""

from typing import Any


def cmd_help() -> Any:
    """
        Print the command reference.
    """

    print("""
        Commands:   
        scan                                Start background scanning
        stop_scan                           Stop background scanning
        list_devices                        List known devices

        connect -mac <ADDRESS>              Connect to a known device (TAB autocompletes)    
        disconnect                          (connected) Disconnect current device

        list_services                       (connected) List GATT services
        list_characteristics                (connected) List ESS (0000fe*) characteristics

        subscribe   -module <temp|imu|fft>  (connected) Read config + subscribe + live plot(s)
        unsubscribe -module <temp|imu|fft>  (connected) Stop notifications + close plots
          
        record      -module <fft> [-out <file.csv>]
                                            (subscribed) Start CSV recording (long format)
        stop_record -module <fft>           (subscribed) Stop CSV recording + close file

        configure -module temp -freq <1..10>
        configure -module imu  -low_scale <2g|4g|8g|16g> 
                               -high_scale <32g|64g|128g|256g>
                               -mode <low|high>
        
        configure -module fft  -axis <x|y|z>
                               -mode <low|high>
                                
                                            (connected) Write sensor config (partial OK)

        clear                               Clear known devices JSON
        help                                Show this help
        exit                                Quit
    """)    # noqa
